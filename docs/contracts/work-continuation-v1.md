# 有来源的任务续接 v1

对应 [#79](https://github.com/destinyTaoTao/evernook/issues/79)。核心保存可核对的任务分支检查点，宿主负责压缩原生聊天、工具历史和模板。检查点不是事实接纳、用户画像、核心人设或工具授权；模型摘要始终是低信任数据。

## 核心接口与来源

`evernook.continuations.Continuations(runtime)` 提供 `save(access, request_id, draft)` 和 `current(access, task_id, branch_id)`。`ContinuationDraft` 包含任务/分支标识、expected_revision、anchor_id、goal、progress、pending、state、expires_at，以及可选的 dependencies、consumed_checkpoints、goal_role。

- goal/progress/pending 使用既有 EvidenceSpan，核对当前状态/内容版本、original 表示、完整且已保存的消息及准确 Unicode code point 原文范围；goal 必须来自真实 user 角色。
- 可信采集身份保存时同时需要 capture 和 recall，读取需要 recall；不需要 accept/admin。权限在来源校验及幂等回读前检查，capture-only 不能借文本匹配差异猜测原文。首个发布者拥有该任务分支的写入权，后续写入使用 revision CAS。其他授权入口可以读取同一分支；推进自己的分支不能覆盖另一个调用端的头版本。
- request_id 与完整输入指纹绑定。重复请求返回同一记录的当前可读视图；改变已用请求的输入会拒绝。新分支/新版本不会通过“最近一条”自动覆盖其他分支。
- 每类 progress/pending 最多32段，单段最多16384 code points，总引用正文最多131072；直接及继承依赖合计最多1024个版本引用，最多消费16个已有检查点。输入与截止时间均严格校验，超限明确拒绝。
- 存储行仅保留标识、版本、范围、状态和有密钥指纹，不重复保存引用正文。读取在一个 SQLite 快照中回源；任何正文/依赖/独立 anchor 失效都返回 source_unavailable，清空正文并披露失效来源，不回退到旧检查点。

state 只接受 unknown、in_progress、completed、abandoned；completed 不能同时声称有待办。显式状态由可信宿主工作流决定，不能从助手输出推断完成。过期返回 expired，废弃返回 abandoned，两者均不自动注入旧正文。任务状态不代替消息的更正/抑制，也不把关闭会话推断为放弃任务。

goal_role 默认 explicit_task_goal。原生 dsh 使用 initial_user_request，明确首条真实用户请求只是初始出处；演变后的目标、当前进展和待办来自有来源的摘要字段，保持 unknown 完成状态。它不把初始请求冒充永远不变的当前目标，也不保证模型对任务的概括正确。

stdio MCP 的 continuation_save/current 仅在 trusted-host profile 注册；普通查询 profile 不能写入或通过伪造参数开启该 profile。原生身份仍只有 capture/recall，跨入口读取不获得维护或事实接纳权限。

## dsh 原生协作

固定 dsh 0.2.1-rc.3。已有 contextConfig + memoryBudget 开启真实目标 tokenizer 路径后，适配器识别成功的原生 compaction/start、summary、end 及 checkpoint source；旧字符模式不声明本契约的预算续接验收。实际压缩继续使用宿主已有 compaction backend；测试锁定 BasicCompaction、TokenMeter，不新增压缩框架或后台服务。

每个成功摘要建立稳定请求身份；身份绑定入口、会话与 compaction ID 的摘要，合法长会话名不会撑破核心标识上限。保存摘要的全部固定段落，包括 Primary Request and Intent、Current Work、Pending Jobs、Critical Context；引用、任务概括和完成猜测都不升级为事实。

辅助 compaction 模型调用也经过敏感历史屏蔽、文件/工具投影和完整请求计数；不是绕过普通对话门禁的旁路。宿主原生 checkpoint 的强背景措辞不直接交给后续 provider，改为本次验证的 untrusted_work_continuation 数据。当前真实用户消息保留，不把摘要当作新用户指令。

依赖覆盖原生所选范围及模型调用实际读取的消息；后者可能含选区之前的前缀。稳定 work-input 记录绑定准确 compaction ID 和实际消费的旧检查点；新检查点继承旧记录的固定版本依赖，而非重新采样成 active。重试不能覆盖同一次压缩已记录的不同输入。可靠流输出通过原生 step/attempt 到核心流的稳定链接回源；缺失链接、未知输入或超限时披露 work_error，不从不确定的旧流猜出完整进展。

fork 的继承前缀沿用父/祖先会话的原始来源身份，最多追溯64层，不把副本重存成不受父来源控制的新证据。原生输入/事件来源协议仍有两处消费，调整该协议时必须共同验证回放与分支关联。

`installMemory(...).work(sessionId)` 读取当前分支；新适配器可先 rescan，再通过原生 resume 加载同一持久会话。适配器只记录、回读和构造数据，不自行改变文件、完成任务或重放旧输出到 UI。

## 预算、遗漏与生效配置

续接使用 memoryTokens 的至多一半作为自身选择额度，与核心记忆合成后再次计数整个 payload 及 SDK section 包装；普通调用和压缩调用还分别验证历史、工具、模板与输出预留的完整请求窗口。已知物理模型窗口与配置窗口取较小值。

先预算全部遗漏定位元数据、合并同一来源的范围，再选择完整正文段落；不截断正文后伪称完整。不足时返回紧凑检查点定位信息。omitted 提供原文 message_id 和 code point 范围；同源合并可能覆盖多个字段，fields 明示范围，普通 read 可核对原文。完整记录可由授权原生客户端调用 continuation_current 回取；这是 trusted-host 读取入口，不虚构普通模型工具。

核心在很小额度下可能返回空 payload 和 context_envelope_over_budget；宿主保留该降级状态，只组合能放下的续接定位信息，不解析空 JSON 或声称核心证据已注入。空 section 不进入 provider。

派发前复核检查点快照。普通调用若恰逢原生压缩或来源控制变化，会重建 payload 并重新计数最多一次；持续变化则阻断。压缩调用在写入消费证明之后复核，变化时不发送旧摘要。它不承诺撤回已经交付的宿主历史或跨进程硬实时取消。

settings().host.compaction 披露后端是否存在、公开数值/布尔配置、精确目标覆盖及宿主预算。BasicCompaction 的触发/保留策略由原生配置控制，使用原生启发式 token meter；真正的最终准入仍使用宿主目标 tokenizer，不能把前者冒称为精确计量。原生检查点 TTL 当前为7天；原生展开至多512个事件，合并依赖仍受核心1024限额约束，实际值可查询。

## 恢复与验证边界

启用本可选表后，任务状态进入认证控制 head、归档控制重放及运行库发布比较。旧记录必须与 live 完全一致，然后才补入最新不可变元数据；新增原文是否完整继续独立检查，不能用元数据充当原文。未使用本功能时不改变旧控制 head。旧快照不能恢复出已经废弃的当前任务；来源控制不完整时继续保留审查限制。

验证覆盖公开 Python、真实 stdio、实际 dsh compaction/partial region/fork/resume、真实 provider 请求和本地 tokenizer。使用合成 provider 与数据，不能作为真实模型概括/回答质量、桌面安装、其他插件组合、系统睡眠/断网、物理掉电、规模或 p95 结论。pi 的压缩续接接线属于 #80；源日志及已交付宿主副本继续按既有残留边界处理。
