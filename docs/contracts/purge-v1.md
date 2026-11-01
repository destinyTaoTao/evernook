# T19 普通来源彻底删除与派生清理 v1

对应 [Issue #20](https://github.com/destinyTaoTao/evernook/issues/20)。本增量实现可信本地的普通来源删除，不把“不再召回”的 `suppress` 改写成销毁，也不开放模型/MCP 删除工具。受保护原件、备份调度、旧备份转正常运行库及宿主外副本治理仍不在本增量内。

## 预览与明确确认

`PurgeWorkflow(runtime)` 复用同一 `MemoryRuntime` 鉴权和 SQLite 账本。

```python
from evernook import PurgeDecision, PurgeWorkflow

workflow = PurgeWorkflow(memory)
preview = workflow.preview(access, "purge-request-1", (message_id,))
# 可信调用端向用户展示 preview 的准确 source_ids、expected_versions 和残留说明，
# 然后另行采集一条真实、完整、原件可见的用户消息作为 confirmation_id。
result = workflow.purge(access, PurgeDecision(preview.request_id, confirmation_id))
```

- 范围为 1–16 个消息 ID，排序去重并绑定每条来源的当前状态版本。相同 request_id/范围重复预览返回原预览；同 ID 不同范围得到 `purge_request_conflict`。
- 预览本身不隐藏、不删除原文，也不阻止相同源事件幂等重试。来源版本变化、范围外 capture/change/replacement 引用或受保护关联会拒绝执行；调用端必须扩大范围或重新预览。
- 执行只接受预览之后接收的活跃 user/complete/original 确认，确认不能在删除范围内。助手声明、旧确认、空文本、投影和无预览请求均不能授权删除。
- 代码验证角色、时间和范围绑定，不能从自然语言证明用户真实意图；可信本地 UI/调用端仍负责展示准确预览并建立用户意图。

CLI 提供同等可信入口，JSON 走 UTF-8 stdin：

```text
python -m evernook.cli purge-preview --config ... --access ...
# {"request_id":"purge-request-1","source_ids":["..."]}
python -m evernook.cli purge --config ... --access ...
# {"request_id":"purge-request-1","confirmation_id":"..."}
```

## 两阶段阻断与恢复

执行先在一个事务中复查确认、版本和依赖，将全部目标置为 `purged`、保存确认及无正文的来源墓碑，然后提交。此后普通读取、搜索、重要交代、候选、事实、经验和上下文均不能取得目标；相同源事件重试得到 `source_purged`，不会因原消息行后来删除而重新采集。

第二阶段先 checkpoint/truncate 旧 WAL，启用 `secure_delete`，再在另一事务中物理删除当前账本内的普通原文、附件、重要交代、任务、派生上下文和索引记录。失败不会撤销第一阶段；使用原 request_id/confirmation_id 重试可续清。进入阻断阶段后不再要求确认消息保持活跃，已完成请求返回稳定回执。

阻断期间（含清理中断后）不允许为删除目标新增引用依赖：新 capture 的 `input_id` 指向已不处于 `active` 状态的消息时得到 `not_recallable`，同源幂等重试仍返回原回执。否则清理中断后新增的依赖会让每次续清都卡在范围复查上，请求永远无法完成。预览后的范围外新引用依旧在执行前复查并得到 `purge_scope_has_dependents`。

`purge_targets_v1`/`purge_requests_v1` 只保留请求、来源身份、HMAC 指纹、版本、确认 ID、状态和回执，不保留被删正文。墓碑会进入之后的普通归档/加密快照，供未来恢复准入核对；本增量没有实现旧备份重放或正常运行库发布。

## 派生传播

- 引用任一目标的候选运行及候选批次整体删除；旧租约不能完成或重建目标。
- 引用目标作为证据或确认的事实决定清除正文、来源片段、候选/处理器、确认及原请求，转为 `fact_tombstones_v1` 中无正文的时间控制记录；只保留事实身份、主体/属性键、修订、时间和替代/解决关系。头版本不回退，删除后的当前/纠错时段返回 `source_unavailable`，不会将旧值恢复为当前事实；仍有效的历史时段及其他主体不受影响。已删除决定的请求 ID 不可复用（`fact_source_purged`）。经验草案/审查记录整体移除。混合来源派生记录不会保留含被删内容的剩余副本。
- 引用目标作为来源或确认的用户画像标签按标签整体移除（含全部修订与审批绑定，审批行带真实外键，先于原文删除）；引用目标的沉淀审查草案同步移除。核心人设版本记录不属于派生资产，不随普通删除改写。
- 引用目标的导航任务立即清空旧摘要并失效；后续有界维护只从剩余及新证据重建。旧 worker 的版本/租约因此不能发布旧摘要。
- 词法 FTS、语义分块、普通派生上下文和附件同步删除。范围外附件即使字节相同也仍是独立证据，不按内容去重误删。

删除使用事务保证当前账本内传播原子性；没有跨宿主缓存或外部服务的撤销能力。

## 残留与未验收边界

每次预览和完成回执都报告：

- `existing_archives_and_backups_retained`：已经存在的普通归档和加密备份不自动改写，仍可包含被删证据；受控备份清理/保留策略另行决定。
- `host_copies_not_controlled`：宿主界面、日志、模型上下文、外部导出和其他副本不受本模块控制。

旧归档恢复仍为 `review_required`，不能借本增量进入普通召回。`secure_delete` 与 WAL 处理是当前 SQLite 账本的清理措施，不声称对文件系统快照、SSD 磨损均衡、内存、崩溃转储或所有历史副本完成取证级擦除。

受保护投影/受保险库关联的来源得到 `protected_purge_unsupported`，不借普通删除绕过 T24 的端到端保护资产治理。重叠预览在其目标已被另一确认请求删除后按已完成处理（no-op 完成），不再永久报 `message_not_found`；阻断期目标对 `change` 不可改写（`not_recallable`），purged 状态不能被改写标签。完整票据仍待可控备份清理策略、最新删除记录重放、运行时恢复及长期/故障矩阵验收。验证见[实施报告](../reports/r3-purge-2026-10-31.md)。
