# T18 有来源经验草案与指定版本审查

对应 [#19](https://github.com/destinyTaoTao/evernook/issues/19)。`evernook.experience.ExperienceRegistry` 保存经验、规则或技能的**文本草案**，不会执行代码、改写系统人设、写 skill 文件、训练权重或授予工具权限。`enabled` 只表示用户审查状态，所有正文仍是不可信记忆数据。

## 草案与修订

`ExperienceDraft(kind, lesson, conditions, sources, disputed=False, processor_version=...)` 的 kind 为 `lesson/rule_draft/skill_draft`，结论与适用条件分别不超过4096/2048 code point，来源是1至16个精确 `EvidenceSpan`。来源状态、表示、版本和 Unicode 坐标必须匹配原话。允许真实 failed/canceled 工具记录支撑失败教训，不将其完成状态改成 complete。引用/角色扮演和助手推断不是事实或权限；回源仍能看到角色、完成状态和时间。

`ExperienceDecision` 使用 request_id、experience_id、expected_revision、action，按作用域 CAS 追加不可变审查记录。重复同内容请求返回原回执，异内容请求冲突；历史回执不把当前状态改回旧版本。重复请求也重验原来源和启用确认，不能取回被抑制的派生文本。

- `propose` 创建首个 draft；`revise` 生成新内容版本并重置为 draft，包括已启用、拒绝或来源失效版本的修订。
- `enable` 仅接受当前 draft/disabled，来源必须有效且无 disputed 标记。修订争议草案后仍需新的审查，不自动解决现实事实争议。
- `reject` 拒绝当前 draft；`disable` 停用当前 enabled。旧内容与决定留作审计，不作为当前规则回退。

revision 包含状态决定；content_revision 只在提议/修订时增加。更正、撤销或抑制来源、或者撤销当前启用确认，读取返回 `source_unavailable` 且不返回派生正文。语义上的新争议无法仅从原文自动判断，可信审查端需明确修订为 disputed；未声称已有争议检测模型。

## 用户确认边界

enable/reject/disable 都需真实、完整、原件可见的用户 confirmation_id；确认的**接收时间**不得早于被展示修订的记录时间。确认首次使用时原子绑定 experience_id、被审查 revision 和 action，不能挪用到另一版本、草案或操作；不得用助手文本或 `approved=true` 绕过。

可信本地调用端仍负责先展示准确版本、建立实际用户意图。角色、时间和引用绑定不能从自然语言证明用户同意。这些写接口供本地维护和可信 UI 使用。独立服务逐次认证调用端并复用同一套确认与版本检查。

## 待审队列

`ExperienceRegistry.queue(access, after=None, limit=16)` 要求 `accept`。每页最多 32 条，按 `experience_id` 升序，以 `next_after` 作为下一页的 `after`。每个 ID 只检查最新版本，当前仍为草案的版本才会列出。分页请求各自读取当前快照。若要查找排在已有游标之前的新草案，从首页重新读取。

条目只返回 ID、审查修订号、内容修订号、状态和错误码。队列在同一 SQLite 快照核验每条来源。争议显示为 `disputed`，来源失效显示为 `source_unavailable`。查看正文与引用时调用 `experience-read`，该入口要求 `recall`，并再次核验来源。可信端应展示读回的准确版本，再发起版本绑定的审查决定。

## 摘录提议与上下文

`propose_navigation(...)` 读取当前有效导航，将有限原文与角色标签拼成“待用户审查与归纳”的 lesson 草案，携带精确来源和调用端给定的适用条件。它不声称已经总结出经验证的规则。来源在读取后变化时提交会拒绝过期草案；导航内容变化却复用旧请求ID会冲突，应显式提交新的请求/修订。

`evernook.skill_drafts.SkillDraftProposer(runtime).propose(access, tool_result_id=...)` 为这条规则提供可信 Python 入口，同时要求 `recall` 和 `accept`。选择来源后，在写事务中重新核验权限、引用版本和精确跨度。每条符合条件的失败或取消工具结果生成一条确定性 `skill_draft`，重试复用该原始提议回执，当前审查状态按注册表读取。

工具结果须是已保存的普通 `tool_result`，并关联同一入口、会话和分支中更早的完整用户输入。两条来源各引用最多 512 个原文字码点。失败或取消导致的 `incomplete_output` 是规则所处理的已知状态，其他采集缺口、受保护投影及派生反思结果会被跳过，返回 `None`。提议仍需用户确认才能启用。原生 dsh 源码提供失败结果映射，取消场景使用 Core Capture 合成回归。[规则测试](../../tests/contracts/test_skill_drafts.py)

## 会话结束与空闲补跑

`skill-draft-configure` 要求 `maintain` 和 `accept`。提供最多 16 个具体 `tool_names` 后进入审查模式。未配置时关闭。启用期间新提交且通过初筛的工具结果在原文提交事务中登记待办。历史结果可由可信 Python 入口显式提议。设置 `enabled=false` 会暂停已登记待办，停用期间的原文仍按正常采集路径保存。

现有调度的 `session_end` 仅处理已有结束标记且早于该标记的同入口和会话结果。`idle` 可补处理其他待办，复用调度的前台让行、暂停、空闲阈值和时限。每次最多处理四条，需要 `maintain`、`recall` 和 `accept`。草案提议与待办出队在同一写事务内完成。中断、并发繁忙或进入前台时，剩余请求保留，下一次有权限的触发可继续。`skill-draft-status` 查看待办数，`experience-queue` 查阅生成的草案。

原文、已生成草案及审查记录进入普通证据归档。技能调度的本机配置和待办只在运行库中生效，恢复后重新配置才会登记新结果。错误的可选策略会在状态查询报告，原始工具结果仍可保存和显式提议。普通沉淀的白名单、显式上下文选择和版本绑定的用户审查继续适用。

最终运行技能、采集、调度、沉淀、归档和独立服务共 78 项契约测试，并通过全仓 strict mypy。真实本机服务使用合成会话；原生 dsh 的完整交互和真实用户 profile 仍按宿主准入范围验收。[会话时序、进程退出和恢复边界回归](../../tests/contracts/test_skill_drafts.py) · [独立服务验证](../../tests/contracts/test_service_cutover.py)

`ContextPolicy.experience_ids` 可显式选择至多64项；仅纳入当前 enabled 版本，位于置顶/事实之后、普通原文之前，计入完整 token 预算。最终 SQLite 快照重验审查修订号和来源；构造途中停用/修订不能留住旧规则。草案、争议、停用或无来源项显示省略原因。known_at 历史查询暂不混入当前经验，报告 `experience_history_unavailable`，不伪装已有历史时点查询。

经验以 `reviewed_experience` 数据进入不可信上下文，`execution_authority=false`。原生 dsh/pi 的完整 context-builder/tokenizer 接线仍待完成，Python 回归不代表原生宿主已自动启用。

## CLI 与只读 MCP

以下命令均带 `--config ... --access ...`，JSON 走 UTF-8 stdin：

| 命令 | 输入 |
|---|---|
| `experience-queue` | 可选的 `after` 和 `limit`，默认 `{}` |
| `experience-decide` | ExperienceDecision；draft.sources 为精确来源数组 |
| `experience-read` | `{"experience_id":"..."}` |
| `experience-from-navigation` | request_id、experience_id、expected_revision、policy_id、bucket、conditions |
| `skill-draft-configure` | `{"tool_names":["synthetic-tool"]}` 或 `{"enabled":false}` |
| `skill-draft-status` | `{}` |

已激活的独立服务通过认证 CLI 提供经验审查和技能草案配置命令。默认与可信 MCP 仍只提供 `evernook_v1_experience(experience_id)` 读取经验。

服务上的 `experience-decide` 回执只含 `experience_id`、`request_id`、`revision`、`content_revision`、`state`、`recorded_at`、`confirmation_id` 和 `confirmation_version`。正文与引用仍通过要求 `recall` 的 `experience-read` 查看。离线 CLI 和 Python 的 `decide` 返回原有完整 `ExperienceRecord`。

生成式经验提议、独立审查 UI、事实争议自动传播、经验历史时点查询和原生规则加载仍待验收。[早期经验契约报告](../reports/r3-experience-2026-10-31.md)
