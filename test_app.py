import base64
import hashlib
import tempfile
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
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")


class MigrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "迁移测试档案", (date.today() + timedelta(days=3650)).isoformat())
        self.store.grant("owner", self.archive["id"], "archivist", "write")
        self.files = [
            {"path": "records/one.xml", "content_b64": base64.b64encode(b"<record><id>1</id></record>").decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ]
        self.version = self.store.ingest_version("owner", self.archive["id"], self.files)

    def tearDown(self):
        self.tmp.cleanup()

    def _html(self, body: bytes) -> str:
        return base64.b64encode(body).decode()

    def test_in_progress_then_complete_with_copies(self):
        first = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html><body><p>1</p></body></html>"),
        )
        self.assertEqual(first["state"], "in_progress")
        self.assertEqual(first["files_done"], 1)
        self.assertEqual(first["files_total"], 2)
        # 没迁完停在迁移中，原格式文件按原件读回
        source = self.store.get_version("owner", self.version["id"])
        self.assertEqual(len(source["files"]), 2)
        self.assertNotIn("migration", source)
        target = self.store.get_version("owner", first["id"])
        self.assertEqual(target["migration"]["state"], "in_progress")
        # 全部迁完 + 副本建好 -> 完整
        self.store.migrate(
            "owner", self.version["id"], "README.txt", "README.html", "html",
            self._html(b"<html><body><p>readme</p></body></html>"),
        )
        self.store.add_copy("owner", first["id"], "offline-disk-a")
        self.store.add_copy("owner", first["id"], "offline-disk-b")
        target = self.store.get_version("owner", first["id"])
        self.assertEqual(target["migration"]["state"], "complete")
        self.assertEqual(target["version"]["state"], "verified")

    def test_concurrent_registration_merges_into_one_batch(self):
        a = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html>a</html>"),
        )
        b = self.store.migrate(
            "archivist", self.version["id"], "README.txt", "README.html", "html",
            self._html(b"<html>b</html>"),
        )
        self.assertFalse(a["already_registered"])
        self.assertTrue(b["already_registered"])
        self.assertEqual(a["batch_id"], b["batch_id"])
        with self.store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM migration_batches WHERE source_version_id=? AND target_format='html'",
                (self.version["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_retry_does_not_rewrite_completed_records(self):
        self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html>a</html>"),
        )
        # 重复提交同一文件：不重复写
        self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html>a-different</html>"),
        )
        self.store.migrate(
            "owner", self.version["id"], "README.txt", "README.html", "html",
            self._html(b"<html>b</html>"),
        )
        with self.store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM migrations WHERE source_version_id=?", (self.version["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 2)
        progress = self.store.get_migration("owner", 1)
        self.assertEqual(progress["files_done"], 2)
        self.assertEqual(progress["files_total"], 2)

    def test_source_change_invalidates_then_reconfirm(self):
        m = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html>a1</html>"),
        )
        self.store.migrate(
            "owner", self.version["id"], "README.txt", "README.html", "html",
            self._html(b"<html>b</html>"),
        )
        self.store.add_copy("owner", m["id"], "offline-disk-a")
        self.store.add_copy("owner", m["id"], "offline-disk-b")
        self.assertEqual(self.store.get_migration("owner", m["batch_id"])["state"], "complete")
        # 源版本后来改动
        new_content = b"<record><id>1-changed</id></record>"
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE archive_files SET content=?, sha256=? WHERE version_id=? AND path=?",
                (new_content, hashlib.sha256(new_content).hexdigest(), self.version["id"], "records/one.xml"),
            )
        recheck = self.store.recheck_migration("owner", m["batch_id"])
        self.assertTrue(recheck["invalidated"])
        self.assertEqual(recheck["state"], "invalidated")
        self.assertEqual(self.store.get_version("owner", m["id"])["version"]["state"], "invalidated")
        # 失效后迁移被拒绝，必须重新确认
        with self.assertRaises(BusinessError) as ctx:
            self.store.migrate(
                "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
                self._html(b"<html>a1</html>"),
            )
        self.assertEqual(ctx.exception.code, "migration_invalidated")
        # 重新确认后继续，已完成记录不重复写
        confirmed = self.store.confirm_migration("owner", m["batch_id"])
        self.assertEqual(confirmed["state"], "in_progress")
        self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            self._html(b"<html>a1-changed</html>"),
        )
        with self.store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM migrations WHERE batch_id=?", (m["batch_id"],)
            ).fetchone()[0]
        self.assertEqual(count, 2)
        self.store.add_copy("owner", m["id"], "offline-disk-c")
        self.assertEqual(self.store.get_migration("owner", m["batch_id"])["state"], "complete")

    def test_version_without_migration_record_still_viewable(self):
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(len(detail["files"]), 2)
        self.assertNotIn("migration", detail)
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertEqual(status["versions"][0]["state"], "verified")
        self.assertNotIn("migration", status["versions"][0])


if __name__ == "__main__":
    unittest.main()
