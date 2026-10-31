# T09 普通证据归档与受控恢复 v1

对应 [Issue #10](https://github.com/destinyTaoTao/evernook/issues/10)。本轮用户确认只交付普通导出、完整性校验与受审查限制的恢复，自动备份继续待办。此接口是可信本地 Python 操作，不注册为 MCP 工具，不新增网络端点。

## 范围与权限

`MemoryRuntime.export_archive(access, destination)` 需要有效身份、契约 v1 和服务端 `archive_review` 权限，因为归档包含已更正、撤销和抑制的原始档案。普通调用端不能通过参数授予自己归档权限。导出前后检查身份及归档权限；目录沿用 Windows 固定本地 NTFS、checkout 外和仅当前账户访问的约束。

归档保留普通账本已经接收的原文或脱敏投影、来源、稳定消息/回执 ID、重要交代、显式变更记录、当前快照状态、工具清单、附件内容与其 SHA-256。当前 SQLite v4 的任务和派生索引也随快照保留，均不因此成为新的权威来源或可运行的消费者。

归档只包含 `manifest.json` 与 `evidence.sqlite3`。不复制 `runtime.json`、客户端令牌、配对文件、HMAC 密钥或保险库材料。已被分流且未保存的敏感原件无法从导出恢复；回执和缺口保持原样。检测规则仍有有限覆盖，归档不增加“所有敏感内容均已识别”的保证。

**普通导出是包含私人证据的未加密文件，只应在用户选择的受保护本地位置使用。** 它不承担加密备份、独立磁盘容灾、运行时身份恢复或既有请求的 HMAC 重试能力。保险库与自动备份保持关闭。

## 调用

以下沿用已初始化的合成本地 `memory/access`，`access` 具有归档审查权限，两个目标目录必须尚不存在，父目录已经存在：

```python
from pathlib import Path
from evernook import restore_archive, verify_archive

exported = memory.export_archive(access, Path(r"E:\evernook-lab\synthetic-export"))
assert exported.recovery_state == "review_required"
verified = verify_archive(Path(r"E:\evernook-lab\synthetic-export"))
archive = restore_archive(Path(r"E:\evernook-lab\synthetic-export"),
                          Path(r"E:\evernook-lab\synthetic-restored"))
assert archive.status() == verified

for message_id in archive.review_message_ids(limit=100, offset=0):
    review = archive.review_message(message_id)
    assert review.archive_only
    # review.state 是归档当时的状态，不证明今天仍允许召回。
```

`EvidenceArchive` 的 `review_message_ids` 按接收时间/ID 分页，最多 1000 个 ID。`review_message` 返回原文、快照状态与变更记录；`review_important` 返回 `ImportantReview(page, state, archive_only=True)`，其 `page` 保持 code point 游标、范围和剩余数，单页 1–8192 个 code point；`review_capture` 返回带快照状态和 `archive_only` 的工具/附件清单审查结果；`review_attachment` 返回归档附件 bytes 或未保存时的 `None`。这些入口均允许明确的归档审查，包括已抑制内容，不能用作普通回答上下文。

归档打开、验证和审查依赖文件所有者的本地访问权限，不依赖已经排除的运行时令牌。调用端应只在可信本地维护流程使用，不将这些入口转交模型或普通远程客户端。

## 一致性、校验和失败

- 使用 SQLite Backup API 获取一致性快照，不直接复制活动数据库主文件；快照分批复制，复制阶段超过 30 秒明确失败，不返回成功。此限制没有经过规模性能校准。
- 先在同一父目录的私有临时目录构建，关闭数据库、同步文件、写清单并校验成功后，以目录重命名发布。目标存在时导出拒绝覆盖；文件错误或进程退出前未发布的目录不视为成功。
- 清单固定归档格式 v1、SQLite schema v4、归档 ID、创建时间、数据库 SHA-256 和消息/重要交代/附件数量。数据库中的 capture 清单保留附件标识、尺寸、SHA-256 和未保存原因，来源映射保留在消息中。
- 验证拒绝未知版本、宽松布尔数字、额外文件、活动 WAL/SHM/journal、目录外文件引用、数据库哈希/数量/结构完整性及外键错误、缺失或损坏的已保存附件。校验使用只读连接，禁用可信 schema；SQL 执行受 30 秒进度预算约束。文件哈希和复制耗时未设整体截止时间。
- `archive_destination_exists` 对任何被占用的还原目标一致返回（含目标存在但不是匹配归档的情形），export/restore 行为一致。常见错误包括 `archive_forbidden`、`unsafe_archive_path`、`archive_destination_exists`、`archive_write_failed`、`archive_snapshot_timeout`、`archive_not_sealed`、`archive_integrity_mismatch`、`attachment_missing`、`attachment_corrupt`、`unsupported_archive` 和 `invalid_archive`，不在错误中回显证据或凭证。
- 同一归档恢复到同一完整目标可幂等返回；不同归档或损坏目标不覆盖。发布前退出后可向原目标重试；退出留下的私有临时目录不自动合并、扫描或提升为成功结果。

SHA-256 检查完整性，不证明发布者身份或状态最新；有能力同时改写数据库与清单的人可以重算哈希。归档内容继续作为不可信数据，不能执行其中的文本或获得工具权限。归档文件应保持封存，打开后每次审查重新校验；不支持其他程序同时改写归档。

## 恢复后的硬边界

`restore_archive` 只向新目录发布可审查的普通证据副本，返回 `EvidenceArchive`。它没有普通 `read/search/submit`，不生成运行时配置、访问凭证或 HMAC 密钥，也没有把 `review_required` 改为可召回状态的接口。源运行时不受恢复影响。

归档可能早于用户后续的更正、抑制或删除。由于本轮没有可证明完整的最新控制记录重放和运行时恢复材料，**所有恢复结果一律保持 `review_required`，不能因快照内部校验通过就开放普通召回。** 保留历史原档用于审查，不表示失效记录可以重新进入回答。

T09 仍未交付加密备份目的地/恢复材料、小时/日调度与保留、恢复后的最新控制记录重放及普通召回准入，也未验收 RPO/RTO、物理掉电或长期运行。相关依赖门禁保持有效；本轮不关闭整张 T09。
