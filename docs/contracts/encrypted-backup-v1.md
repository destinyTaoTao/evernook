# T09：加密快照、受控恢复与保留 v1

对应 [Issue #10](https://github.com/destinyTaoTao/evernook/issues/10)。备份路径可配置，本轮按用户授权仅使用 E 盘临时合成目录验证；它不代表独立磁盘容灾或正式计划已启用。

## 密钥和入口

安装可选依赖 `.[backup]`（PyNaCl 1.6.2）；普通核心不导入该依赖，开发完整回归使用 `.[dev]`。公开 API 位于 `evernook.backup`，不作为 MCP 工具开放。

- `initialize_backup_keys(new_directory, password_bytes)`：可信本地初始化新私有目录。生成接收密钥对和独立签名密钥；接收私钥由 Argon2id interactive 参数派生的 SecretBox 密钥保护。只返回公钥/恢复文件路径及 key ID；口令不落盘，已有目录拒绝覆盖。
- 备份写入使用 `receiving.json` 的接收公钥和 ACL 保护的 `writer.key` 签名能力，不持有接收私钥。`recovery.json` 包含经口令保护的接收私钥与可信发送验证公钥，需由用户独立保护。没有口令和有效恢复材料时不能绕过解密。
- `create_backup(memory, access, new_destination, key_directory)`：要求当前 `archive_review` 权限；使用现有 SQLite Backup API 一致性快照，不直接复制活动主文件。返回 BackupStatus，明确 `schedule_enabled=false`。
- `restore_backup(source, new_destination, recovery_file, password_bytes)`：验证密钥、发送签名、各文件哈希、流认证/完成标记和原归档完整性，恢复为 `EvidenceArchive`。不发布令牌、HMAC 材料或旧运行身份，不自动开放普通召回。

每个文件独立使用 libsodium XChaCha20-Poly1305 secretstream 随机密钥、SealedBox 包装的文件密钥和固定对象/密钥/文件绑定 AAD；外层 Ed25519 签名绑定备份 ID、格式、文件密文哈希及封装信息。数据按 1 MiB 帧加密，必须存在已认证的 FINAL 标记且其后无多余字节。不得以重新计算普通哈希绕过认证、删除尾部或交换帧。

目的目录只发布 `backup.json` 和三份 `.enc`。普通快照的 manifest、SQLite 和运行身份/HMAC 恢复材料都加密；口令/恢复私钥不放进备份包。普通原文快照暂存在源运行目录旁的新 owner-only 私有目录，加密成功后清理；强杀可能留下私有临时目录，不能把它视为已发布备份或声称安全擦除。原普通账本本身仍未静态加密。

恢复时即使整包认证通过，也不能证明快照之后的更正/抑制/删除记录完整。因此结果保持 `review_required`；旧 HMAC/身份只在内存验证，不复制进审查归档。最新控制记录及普通运行时恢复准入仍需后续实现，不能用加密成功代替遗忘完整性。

## 保留与恢复锚点

`retain_backups(root, key_directory, recovery_verified_id=..., now=None, apply=False)` 默认只生成计划：

- 保留最新快照、滚动 48 小时中每小时的最新实际快照、滚动 30 天中每 24 小时的最新实际快照，以及用户已真实恢复验证的锚点 ID。
- 缺少对应锚点时拒绝。这个 ID 必须由可信本地维护流程在实际恢复后指定，普通模型不能声明自己已经验证。
- 仅考虑当前受信任密钥签名、完整密文哈希、同一根目录下的已知格式包；外部链接、其他密钥版本、未来时间、损坏/不支持包和其他文件保留并报告 skipped。
- 显式 `apply=True` 才执行删除；删除前再次核对绝对路径和清单哈希，不能清掉唯一恢复锚点。旧密钥包不会被当前密钥的保留过程盲删。

这实现保留能力，不证明已经按小时/日生成足够快照。当前没有注册计划任务，也未验收目的地独立性、离线告警、RPO/RTO 或长期运行。

## CLI

新库可在 `evernook init` 时显式加 `--allow-archive-review` 授予该本地 owner 归档/备份权限；默认不授予。已有身份权限沿原契约，不因安装依赖自动提升。

```text
evernook backup-init --key-dir E:\evernook-lab\synthetic-backup-keys
evernook backup-create --config E:\EverNookData\runtime.json --access E:\EverNookData\mcp-access-owner.json --key-dir E:\evernook-lab\synthetic-backup-keys --destination E:\evernook-lab\synthetic-encrypted-snapshot
evernook backup-restore --source E:\evernook-lab\synthetic-encrypted-snapshot --destination E:\evernook-lab\synthetic-reviewed-restore --recovery-file E:\evernook-lab\synthetic-backup-keys\recovery.json
```

初始化与恢复只在真实本地 TTY 中隐藏输入口令；拒绝通过管道、argv、环境变量或 MCP 参数传入口令。保留规划：`evernook backup-retain --root <snapshot-parent> --key-dir <keys> --recovery-verified-id <verified-backup-id>`，检查计划后显式加 `--apply` 执行既定保留策略。

私有目录 ACL 不隔离已控制同一 Windows 账户的进程。Python 释放引用不保证所有口令/私钥副本清零；用户仍需保护操作系统、进程内存、恢复材料与独立备份盘。当前加密实现复用 [PyNaCl secret/public/password API](https://pynacl.readthedocs.io/en/latest/)，不自行设计加密算法。
