# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：为源版本登记迁移任务并生成目标版本；同一源版本重复登记时返回已有任务和进度（先登记者继续，后到者确认后协作）。
- `GET /api/migrations/{id}`：查看迁移任务进度、逐文件记录和副本状态。
- `POST /api/migrations/{id}/files`：迁移单个文件到目标版本；中途失败重试时已完成的文件跳过、不重复写。
- `POST /api/migrations/{id}/confirm`：源版本改动导致任务失效后，重新确认并恢复迁移。
- `GET /api/archives/{id}/status`：保留期限、版本状态（含迁移状态）和审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

迁移目标版本在全部文件迁完、副本逐份校验通过且至少建好一个独立副本后才标记为 `complete`，此前保持 `migrating`；迁移中读取目标版本或新建副本时，未迁移的文件按源版本原件读回。源版本降级等改动会使由它迁出的任务失效（`invalidated`），源版本恢复健康副本后需重新确认。没有迁移记录的历史版本一律视为 `unmigrated`，查看与校验不受影响。
