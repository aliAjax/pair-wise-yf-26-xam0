import base64
import hashlib
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.readme = b"archive readme"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(self.readme).decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    def _migrate_all_files(self) -> dict:
        job = self.store.start_migration("owner", self.version["id"])
        self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html",
                                self._b64(b"<html><body><p>1</p></body></html>"))
        self.store.migrate_file("owner", job["id"], "README.txt", "README.txt", "text", self._b64(self.readme))
        return job

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])

        job = self.store.start_migration("owner", self.version["id"])
        self.assertFalse(job["already_registered"])
        self.assertEqual(job["state"], "migrating")
        target_id = job["target_version_id"]

        self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html",
                                self._b64(b"<html><body><p>1</p></body></html>"))
        detail = self.store.get_version("owner", target_id)
        self.assertEqual(detail["version"]["version"], 2)
        self.assertEqual(detail["migration_status"], "migrating")
        by_path = {f["path"]: f for f in detail["files"]}
        self.assertTrue(by_path["records/one.html"]["migrated"])
        # 未迁移的文件按源版本原件读回
        self.assertFalse(by_path["README.txt"]["migrated"])
        self.assertEqual(by_path["README.txt"]["origin"], "source_version")
        self.assertEqual(by_path["README.txt"]["sha256"], hashlib.sha256(self.readme).hexdigest())

        self.store.migrate_file("owner", job["id"], "README.txt", "README.txt", "text", self._b64(self.readme))
        # 文件迁完但还没有独立副本，不能算完整
        self.assertEqual(self.store.get_migration("owner", job["id"])["state"], "migrating")

        copy_id = self.store.add_copy("owner", target_id, "offline-disk-c")["id"]
        self.assertEqual(self.store.verify_copy("owner", copy_id)["state"], "healthy")
        final = self.store.get_migration("owner", job["id"])
        self.assertEqual(final["state"], "complete")
        self.assertEqual(final["progress"], {"total_files": 2, "migrated_files": 2})
        detail = self.store.get_version("owner", target_id)
        self.assertEqual(detail["migration_status"], "complete")
        self.assertTrue(all(f["migrated"] for f in detail["files"]))

        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)
        states = {v["id"]: v["migration_status"] for v in status["versions"]}
        self.assertEqual(states[target_id], "complete")
        self.assertEqual(states[self.version["id"]], "unmigrated")

    def test_concurrent_migration_registration_first_wins(self):
        self.store.grant("owner", self.archive["id"], "archivist", "write")
        results, barrier = [], threading.Barrier(2)

        def start(actor):
            barrier.wait()
            results.append(self.store.start_migration(actor, self.version["id"]))

        threads = [threading.Thread(target=start, args=(actor,)) for actor in ("owner", "archivist")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 先登记者创建任务，后到者拿到同一任务和进度
        self.assertEqual(sorted(r["already_registered"] for r in results), [False, True])
        self.assertEqual(len({r["id"] for r in results}), 1)
        late = next(r for r in results if r["already_registered"])
        self.assertEqual(late["progress"], {"total_files": 2, "migrated_files": 0})
        # 后到者确认进度后可以继续协作
        confirmed = self.store.confirm_migration("archivist", late["id"])
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(confirmed["state"], "migrating")

    def test_resume_after_failure_skips_completed_files(self):
        job = self.store.start_migration("owner", self.version["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.migrate_file("owner", job["id"], "records/one.xml", "README.txt", "text", self._b64(b"x"))
        self.assertEqual(ctx.exception.code, "target_path_conflict")

        first = self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html",
                                        self._b64(b"<html/>"))
        self.assertFalse(first["already_migrated"])
        # 迁移中途失败：第二个文件内容非法，任务停在迁移中
        with self.assertRaises(BusinessError):
            self.store.migrate_file("owner", job["id"], "README.txt", "README.txt", "text", "!!!not-base64!!!")
        self.assertEqual(self.store.get_migration("owner", job["id"])["state"], "migrating")
        # 重试时已完成的文件跳过，记录不被改写
        retry = self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html",
                                        self._b64(b"<html>changed</html>"))
        self.assertTrue(retry["already_migrated"])
        self.assertEqual(retry["sha256"], first["sha256"])
        done = self.store.migrate_file("owner", job["id"], "README.txt", "README.txt", "text", self._b64(self.readme))
        self.assertFalse(done["already_migrated"])
        view = self.store.get_migration("owner", job["id"])
        self.assertEqual(view["progress"], {"total_files": 2, "migrated_files": 2})
        files = {f["path"]: f for f in self.store.get_version("owner", job["target_version_id"])["files"]}
        self.assertEqual(files["records/one.html"]["sha256"], hashlib.sha256(b"<html/>").hexdigest())

    def test_copy_created_mid_migration_stays_in_sync(self):
        job = self.store.start_migration("owner", self.version["id"])
        self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html",
                                self._b64(b"<html/>"))
        # 迁移中建副本：已迁移文件 + 未迁移文件的源版本原件
        copy_id = self.store.add_copy("owner", job["target_version_id"], "offline-disk-c")["id"]
        with self.store.connect() as conn:
            paths = {r["path"] for r in conn.execute("SELECT path FROM copy_files WHERE copy_id=?", (copy_id,))}
        self.assertEqual(paths, {"records/one.html", "README.txt"})
        # 继续迁移剩余文件，副本同步换入新内容
        self.store.migrate_file("owner", job["id"], "README.txt", "README.txt", "text", self._b64(self.readme))
        with self.store.connect() as conn:
            rows = {r["path"]: r["sha256"] for r in conn.execute("SELECT path,sha256 FROM copy_files WHERE copy_id=?", (copy_id,))}
        self.assertEqual(rows["README.txt"], hashlib.sha256(self.readme).hexdigest())
        self.assertEqual(self.store.verify_copy("owner", copy_id)["state"], "healthy")
        self.assertEqual(self.store.get_migration("owner", job["id"])["state"], "complete")

    def test_source_change_invalidates_derived_migration(self):
        job = self._migrate_all_files()
        target_copy = self.store.add_copy("owner", job["target_version_id"], "offline-disk-c")["id"]
        self.store.verify_copy("owner", target_copy)
        self.assertEqual(self.store.get_migration("owner", job["id"])["state"], "complete")

        # 源版本所有副本损坏 → 源版本降级 → 派生迁移任务失效
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        self.store.simulate_corruption("owner", self.copy2, "records/one.xml")
        self.assertEqual(self.store.verify_copy("owner", self.copy1)["state"], "degraded")
        self.assertEqual(self.store.get_migration("owner", job["id"])["state"], "invalidated")
        self.assertEqual(self.store.get_version("owner", job["target_version_id"])["migration_status"], "invalidated")
        with self.assertRaises(BusinessError) as ctx:
            self.store.migrate_file("owner", job["id"], "records/one.xml", "records/one.html", "html", self._b64(b"<html/>"))
        self.assertEqual(ctx.exception.code, "migration_invalidated")
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_migration("owner", job["id"])
        self.assertEqual(ctx.exception.code, "source_degraded")

        # 源版本恢复健康副本后重新确认，迁移任务恢复完整
        healed = self.store.add_copy("owner", self.version["id"], "offline-disk-d")["id"]
        self.assertEqual(self.store.verify_copy("owner", healed)["state"], "healthy")
        confirmed = self.store.confirm_migration("owner", job["id"])
        self.assertEqual(confirmed["state"], "complete")

    def test_legacy_version_without_migration_records(self):
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        detail = self.store.get_version("auditor", self.version["id"])
        self.assertEqual(detail["migration_status"], "unmigrated")
        self.assertIsNone(detail["migration"])
        # 旧版本没有迁移记录，查看和校验不受影响
        self.assertEqual(self.store.verify_copy("auditor", self.copy1)["state"], "healthy")
        status = self.store.archive_status("auditor", self.archive["id"])
        self.assertEqual(status["versions"][0]["migration_status"], "unmigrated")

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.start_migration("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")


if __name__ == "__main__":
    unittest.main()
