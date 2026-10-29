# T07 更正、撤销与不再召回

对应 [#8](https://github.com/destinyTaoTao/evernook/issues/8)。在统一 Python 边界增加消息级显式变更、归档审查与派生上下文发布屏障；没有引入模型、事实抽取或任务消费者。所有文本和第三方结果都是数据，不授予执行能力或修改核心人设。

## 显式请求与版本

先用普通原文接口保存完整的用户更正/撤销/抑制指令，再提交：

```python
from evernook import Change

state = memory.change(access, Change(
    "synthetic-change-1", old_receipt.message_id, 1, "correct",
    correction_receipt.message_id, "synthetic correction"))
assert state.version == 2 and state.status == "corrected"
assert memory.read(access, old_receipt.message_id) is None
assert state.replacement_id == correction_receipt.message_id
```

`action` 仅接受 `correct/revoke/suppress`，分别将目标置为 `corrected/revoked/suppressed`。更正以新证据消息为替代引用，不覆盖或拼改旧原件，也不推断历史有效期。撤销指撤销目标陈述，当前不提供恢复旧内容或撤销一次变更的操作；需要新的显式信息可另存原文。

来源状态从 `active,version=1` 起。`expected_version` 是目标**状态版本**，与不可变原文的 `content_version=1` 不同；变更可靠提交后递增。所有请求含稳定 `request_id`，同 ID 同完整请求返回原历史回执；同 ID 异内容报 `change_conflict`，不同请求使用旧版本报 `version_conflict`。旧回执重试不改变当前新状态，应另用 `state(access,id)` 读取现状。

每项变更保存来源证据 ID、简短原因和 UTC 记录时间。证据必须存在、含非空白文本、为 `user/complete`、原件已保存且当前可用，不能以目标自身、投影、助手输出或未完成片段作为确定更正的来源；否则 `insufficient_evidence`。目标不存在、未知操作、非法版本和含已知敏感信号的原因等明确拒绝。这里只核对证据资格与用户显式指向，不声称判断自然语言的事实真伪或自行解决歧义。

## 普通读取与归档

变更、来源和当前状态在同一事务提交。确认后，`read/read_source/list_messages` 不再返回被更正/撤销/抑制目标；已有关联的重要条目回取及再标记报 `not_recallable`，引用本身仍留存。普通读取不会自动把旧 ID 的原话替换为新句子；更正应根据 `state.replacement_id` 单独读取当前仍可用的新证据，不自动追链或推断有效事实。新证据后来不可用时应显示缺口。

同源原文重试仍返回历史持久化回执，但不能重置可见状态。不再召回不等于物理删除；`status` 的消息/标记计数包含归档记录。列表的时间和分页在过滤后计算，不因隐藏目标而错误截短一页。

`review_archive(access,id)` 是独立权限与返回类型：初始化时可信操作端显式选择 `allow_archive_review=True` 才授予该身份归档审查权限；默认拒绝，普通请求参数不能授予自己权限。返回 `ArchiveView(archive_only=True)`，包含原始消息、当前状态和按提交顺序的完整变更来源/原因。仅供审查，不供回答上下文；保险库关闭时归档也只有已经保存的投影，不能恢复未保存秘密。

运行配置仍 v1；新建身份文件使用严格 v2（新增服务器授予的 bool `archive_review`），旧 v1 身份兼容读取但不会自动获得归档权限。权限材料保留在当前用户专用目录，实际账户隔离边界同 T05，尚无配对/角色管理 UI。T07 存储 v3 补齐状态、变更及派生发布表，当前 [T08](capture-v1.md) 升至 v4；事务兼容补表，保留原始消息与回执。

## 旧任务与派生结果

`publish_context(access, DerivedContext(result_id,source_id,source_version,text))` 是最小结果发布屏障，不是抽取流水线。调用方开始处理时取得 `state.version`，结束后必须用原版本提交；核心在短写事务内再次核验来源仍 `active` 且版本一致，否则 `stale_source`。结果 ID 同内容幂等、异内容冲突；声明/检测为敏感的结果拒绝，投影来源只能发布同一安全占位文本。原文、身份等输入约束仍适用，不运行模型或外部动作。

`read_context` 每次用持久化来源状态与版本过滤。已经提交的旧派生结果会在更正/撤销/抑制后立即不可读；挂起的旧处理即使重启、晚交或重试也不能恢复目标。仅缓存一次核验结果是不够的；以后索引、摘要、缓存适配必须复用该屏障与读侧核验，不能宣称尚未实现的外部组件已覆盖。派生文字不是用户原话或已核实事实。

并发读取在线性化到自身数据库快照时观察状态；已在变更之前完成读取并交给调用端的内容无法追溯撤回，调用端复用旧缓存前仍须重新核验。当前没有硬删除、长期备份或人格变更接口；未解除备份、模型和 dsh 实际显示集成的门禁。
