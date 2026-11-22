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

已激活的独立服务通过认证 CLI 提供 `experience-queue`、`experience-read` 和 `experience-decide`。默认与可信 MCP 仍只提供 `evernook_v1_experience(experience_id)` 读取经验。

服务上的 `experience-decide` 回执只含 `experience_id`、`request_id`、`revision`、`content_revision`、`state`、`recorded_at`、`confirmation_id` 和 `confirmation_version`。正文与引用仍通过要求 `recall` 的 `experience-read` 查看。离线 CLI 和 Python 的 `decide` 返回原有完整 `ExperienceRecord`。

尚无自动经验生成模型准入、后台提议调度、独立审查 UI、事实争议自动传播、经验历史时点查询或原生规则加载，完整票据保持开放。验证见[报告](../reports/r3-experience-2026-10-31.md)。
