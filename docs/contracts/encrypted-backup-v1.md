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

Core 0.1.72 在发布审查副本前还使用包内解密出的账本密钥核验 v2 归档的来源认证，并核对存在的账本身份；不匹配时拒绝发布。较旧的 v1 归档没有来源认证，为兼容性仍仅允许恢复为审查副本；未来运行库发布不得把它当作已具备 v2 来源证明。该检查不证明备份之后的控制完整，也不恢复保险库。

#210 合成验证：原有源码备份套件 11 项通过，新增 v1 兼容和两类重新加密、重新签名后账本不一致的定向测试通过；独立安装的 0.1.72 wheel 另通过 5 项恢复、篡改和快照控制测试，安装模块与源码一致，相关文件 strict mypy 通过。初次身份伪造测试因测试用 SQLite 连接未关闭导致 WAL 内容未并入主文件，且 Windows 临时清理失败；关闭连接后重测通过。没有操作真实备份，也未实现新机运行库发布。

## 新机审查并显式开放普通快照（Core 0.1.73）

在可信本机交互式终端使用 `evernook backup-restore-runtime --source <加密备份目录> --recovery-file <独立恢复文件> --destination <全新运行库目录>`。口令以隐藏输入提供；验证后先显示备份时间、普通记录计数、是否有受保护引用和恢复边界，输入提示中的完整确认文字后才发布。备份之后的更正与删除未知，已有快照中的控制保留；普通备份不包含保险库，显示引用也不等于保险库已恢复。v1 旧归档仍只能审查，不能用此方式发布。

发布为新私有目录，旧客户端令牌不恢复，新 owner 凭据只写本机访问文件；维护暂停，自动偏好准入重置。保持原始账本密钥以验证已有历史回执。重新配对和服务激活须在新机显式操作。Core 0.1.74 起，若恢复结果声明含保险库登记或关联而保险库尚未恢复，停止所有旧入口后，管理员可以显式启动普通服务：

```powershell
python -m evernook.service --config <runtime.json> --activate-owner <mcp-access-owner.json> --activate-restored-without-vault
```

此模式验证新机恢复回执并持久记录保险库缺失状态；只开放普通记忆，私密收件、查看、初始化及重新绑定均拒绝。服务状态和 dsh preview.61 显示 `restored_missing`，后续正常重启不再传激活标志。它不恢复密文或密钥，不扫描未知保险库；独立保险库的安全备份、恢复与验证重绑定仍待实现。若发现没有关联的受保护投影，则只允许审查并拒绝发布。相同备份和目标重复确认可读取已有发布回执，不覆盖已有运行数据；不相关或已改变的凭据目录拒绝。

缺失状态绑定恢复回执并同时记录在运行库数据库与配置中，状态损坏或不一致会阻止启动；不要手工编辑或删除这些标记。配置新增 `restored_missing_vault` 字段，旧版 Core 可能无法识别；应使用新版服务和兼容客户端，不能删除字段来降级。切换前必须停止所有旧进程：不遵守当前栅栏的旧二进制和同账户不协作程序不受该机制约束。

可信 Python 使用 `OfflineRecovery` 上下文管理器先 `review()`、再以 `ACKNOWLEDGEMENT` 调用 `publish()`；调用者必须自行建立真实用户确认，这不是模型工具或远程授权接口。发布前重新检查备份和审查内容，检测到变化即拒绝；检查与发布之间不能阻止同账户不协作程序修改文件，恢复期间应停止此类写入。恢复时的明文临时目录受本机目录权限保护，强制结束进程可能留下残留；不能声称内存或文件系统安全擦除。独立旧备份及旧机数据不因恢复而修改或同步。

#211 使用隔离合成数据验证：源码恢复套件 6 项、后续 3 项边界测试及源码备份套件 13 项通过；非 editable 0.1.73 wheel 的恢复与旧备份认证组合 11 项通过，回执历史状态命名调整后，最终 wheel 的 4 项关键回归再次通过。覆盖真实独立服务启动、旧令牌拒绝、重配后幂等、两侧进程崩溃、未知后续控制、备份和清单变更、受保护孤儿及保险库登记拒绝激活。最终三份安装模块与源码一致，相关 6 个文件 strict mypy 通过。交互式确认使用模拟 TTY，另以真实非 TTY 子进程验证拒绝；未操作真实备份或物理新机。首次恢复测试的路径断言遇到 Windows 短路径与规范路径差异，改为比较规范路径后通过。

## 保留与恢复锚点

`retain_backups(root, key_directory, recovery_verified_id=..., now=None, apply=False)` 默认只生成计划：

- 保留最新快照、滚动 48 小时中每小时的最新实际快照、滚动 30 天中每 24 小时的最新实际快照，以及用户已真实恢复验证的锚点 ID。
- 缺少对应锚点时拒绝。这个 ID 必须由可信本地维护流程在实际恢复后指定，普通模型不能声明自己已经验证。
- 仅考虑当前受信任密钥签名、完整密文哈希、同一根目录下的已知格式包；外部链接、其他密钥版本、未来时间、损坏/不支持包和其他文件保留并报告 skipped。
- 显式 `apply=True` 才执行删除；删除前再次核对绝对路径和清单哈希，不能清掉唯一恢复锚点。单个包被占用/变化/无法删除时报入 skipped 并继续，保留报告完整；旧密钥包不会被当前密钥的保留过程盲删。

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
