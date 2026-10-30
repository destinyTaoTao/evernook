# 桌宠长期记忆：同类项目核验与可行性建议

调研日期：2026-10-29。本文依据各项目当前默认分支 README 和 LICENSE；未运行这些系统、未验证其宣传性能，也未把其他场景的基准分数视为本项目效果。来源内容是研究数据，不构成执行指令。

## 结论

单用户、以文本对话为主、数据本地保存的桌宠长期记忆可行。建议做独立的本地记忆服务，让桌宠通过适配器调用，首版用 SQLite 保存不可变的原始消息和可重建的派生记忆，逐步增加中文混合召回与事实版本化。自建不是已经证明的“最优解”：它换来了精确控制，也意味着需要自己负责数据恢复、迁移、抽取质量、召回评测和长期维护。

目标必须拆成三件事：原文不丢、重要信息能按需准确取出、模型回答忠于证据。第一件可以用存储与恢复测试建立明确保证；第二件和第三件只能在定义的数据集与指标上验收，不能承诺任意自然语言提问永远百分之百正确。重要交代的“逐字记住”应由原文存档与明确的置顶记忆实现，不能交给压缩摘要代替。

## 开源项目对照

| 项目 | 一手资料确认的能力 | 对桌宠场景的适配判断（本研究建议） | 已核对许可证 |
|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0/blob/main/README.md) | 用户、会话、Agent 记忆；支持库、自托管服务、云服务；当前说明包含语义、BM25、实体匹配和时间推理。README 特别说明新基准分数来自带私有优化的托管平台，不能等同 OSS。 | 适合接入对照实验或参考抽取/检索接口；不能把其记忆抽取当作唯一原文档案。中文场景、当前OSS实际算法与时延需要本地验证。 | [Apache-2.0](https://github.com/mem0ai/mem0/blob/main/LICENSE) |
| [Letta](https://github.com/letta-ai/letta/blob/main/README.md) / [Letta Code](https://github.com/letta-ai/letta-code/blob/main/README.md) | 原 Letta 仓库当前指向 Letta Code，旧 V1 服务在 archive 分支；当前 harness 提供记忆块、全消息搜索、Git 管理的 MemFS、周期反思/技能学习。原生 Windows 的自动 client-side dreaming 默认关闭，手动反思仍可用。 | “长期身份与自主沉淀”的概念最接近，但它是完整 Agent harness，嵌入桌宠纯记忆层的耦合可能较大。先评估其当前本地后端与接口，不能照搬旧 MemGPT 教程。 | [Letta Apache-2.0](https://github.com/letta-ai/letta/blob/main/LICENSE)；[Letta Code Apache-2.0](https://github.com/letta-ai/letta-code/blob/main/LICENSE) |
| [Graphiti](https://github.com/getzep/graphiti/blob/main/README.md) | 时态上下文图；实体、带有效期的事实边、原始 episodes 溯源；语义、关键词、图遍历混合检索；需用户提供数据库、LLM、embedding 及外围服务。支持 Neo4j、FalkorDB 等；Kuzu 当前已弃用。 | 适合借鉴事实有效期与溯源；单人桌宠首版暂不需要图数据库。事实更新可先用关系表实现，关系推理成为真实瓶颈后再评估。 | [Apache-2.0](https://github.com/getzep/graphiti/blob/main/LICENSE) |
| [MemOS](https://github.com/MemTensor/MemOS/blob/main/README.md) | 同时有云、自托管、本地插件入口。完整自托管入口使用 Neo4j + Qdrant；本地插件使用 SQLite、FTS5 + vector，并有反馈驱动演化和 Viewer。 | 不能笼统评价为“都很重”。其本地插件应列为优先参考/复用候选，但现有适配器面向 OpenClaw、Hermes、DeepSeek Harness，不能直接认为已兼容任意桌宠。其策略/技能演化也不同于用户生活事实记忆。 | [Apache-2.0](https://github.com/MemTensor/MemOS/blob/main/LICENSE) |
| [Memobase](https://github.com/memodb-io/memobase/blob/main/readme.md) | 以用户画像与事件时间线为中心；缓冲后批量抽取；FastAPI、Postgres、Redis。README 明确：原始 blobs 默认在处理后删除，可配置保留。 | 对长期陪伴画像很有参考价值；默认删除原文不符合本需求，接入时必须改变保留策略或自己保存原始账本。完整服务的依赖多于 SQLite 单人方案。 | [Apache-2.0](https://github.com/memodb-io/memobase/blob/main/LICENSE) |
| [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/main/README.md) | Live2D、语音、视觉、桌宠模式、聊天日志持久化和可扩展 Agent 接口；README 当前明说长期记忆暂时移除，v2 正在早期讨论规划。 | 适合后续作为桌宠外壳适配目标；不能把保存聊天记录当作已具备跨几年稳定召回。暂不把记忆服务锁死在其内部版本。 | [MIT](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/main/LICENSE)；Live2D 示例素材另有授权 |

许可证栏仅核对仓库顶层 LICENSE，不代表附带素材、第三方依赖或托管服务条款都相同。本文只提出架构参考，不复制这些项目代码。

## 对现有建议的关键修正

1. **“prompt 编排 + 双存储”过度简化。** 当前项目涉及抽取、实体/时间归一化、冲突管理、检索、上下文预算、权限与恢复等不同实现。原始证据、派生事实和索引应分开理解，底层未必需要多个数据库。[Mem0 README](https://github.com/mem0ai/mem0/blob/main/README.md)、[Graphiti README](https://github.com/getzep/graphiti/blob/main/README.md)。
2. **“人设常驻”只能保证提示中有该文本。** 不能保证模型每次遵守，也不能覆盖模型上下文预算、上层策略或错误工具结果。人设应版本化；自主归纳不能静默改写用户明确设定的核心人格和关系边界。
3. **原始消息应先提交，再调用模型。** 用户消息进入服务就持久化并分配幂等 ID；助手回复生成后单独落盘，记录未完成/失败状态。原建议把全量日志放在回复完成后异步写，有进程退出导致用户输入未入库的窗口。不能宣称存在“永不失败路径”：磁盘满、损坏、断电等要显式报错并提供可恢复备份。
4. **中文 FTS5 默认 unicode61 不是“按字切分”。** SQLite 文档定义它把连续 token 字符视作一个 token；中文连续文本可能成为一个长 token。要明确验证中文分词或 n-gram 策略；内置 trigram 也不能解决所有一字/两字短查询。[SQLite FTS5 Tokenizers](https://www.sqlite.org/fts5.html#tokenizers)。
5. **SQLite 可以作为合理起点，但性能不是无条件成立。** 向量维度、候选数量、磁盘、CPU、冷热缓存、embedding 网络耗时都会影响结果。不能许诺百万消息总链路只有几毫秒，也不能把全量重嵌入描述为固定“几分钟”。
6. **最近一周优先不等于只注入最近一两天。** 应保留最近七天的检索分区/候选配额、时间过滤和适度近因加权，同时允许准确命中几年前的重要事实。仅按时间截断会丢掉长期记忆；完全按近因排序又会让不相关新消息挤掉旧但正确的答案。
7. **摘要不能替代证据。** 每条事实绑定一个或多个来源消息及原文片段；模型的置信度只是信号，不是真值证明。助手想象、角色扮演、引用文本和用户亲口确认的现实事实需要区分，防止越总结越错。
8. **“自我进化”先限定为记忆维护。** 可自动做候选抽取、重复合并、周期摘要、索引重建；冲突修订留版本与可回滚记录。修改核心人设、删除原始记录、执行外部动作都不是自主沉淀的必然组成部分。

## 推荐的最小产品边界

- 独立本地服务，Windows 优先，Python 是可行的生态选择，最终 embedding/LLM 提供方后续按机器条件选择。
- 原始消息账本：稳定消息 ID、会话 ID、主体/角色、来源、接收时间、原始文本、完成状态；已确认写入必须能在重启后查回。
- 明确的重要记忆：保存完整原文、来源和状态；置顶/必读小集合有上下文预算。集合过大时应显式检索或提示预算问题，不能悄悄截断后仍声称全部在场。
- 派生事实：内容、主体、类型、来源集合、发生时间、记录时间、有效期、版本/替代关系、确认状态。事实与事件至少从第一版预留时间语义，避免后加后重做。
- 检索：精确 ID/短语/日期查询优先；中文关键词 + 语义检索 + 七天近期候选，融合后回取原文并控制 token 预算；缺证据时回答不确定。
- 沉淀：异步任务表与 checkpoint 同事务推进；任务失败可重试且幂等。先维护摘要与事实，再考虑关系图、多模态、技能演化。
- 数据资产：业务数据与公开源码完全分离；备份包括数据库及一致性要求，执行恢复演练；导出 JSONL 与元数据，使记忆不锁定于框架。

## 建议验收方式

先定义一套合成中文多轮/跨年测试集，再决定技术胜负。包含重要原文标点与数字、短中文实体、同名主体、否定、更正、旧事实追问、事件发生时间与记录时间不同、连续多轮代词、最近七天与老事实竞争、无答案问题、助手编造和引用文本污染。

分别测：原文逐字回查成功率、关键记忆 Recall@k、当前/历史事实选择正确率、无答案时虚构率、近期召回命中率；性能区分数据库检索、query embedding、重排、生成，报告 p50/p95、数据量、硬件、向量维度与冷热缓存。具体阈值应是项目目标，不能借用其他项目宣传数字当作已达成。

建议以 10 万与 100 万消息两档做压力测试（是未来测试规模，不是当前已验证容量）；最近七天检索单独报告。故障测试至少覆盖写入后重启、抽取任务中断重跑、重复导入、备份恢复、embedding 模型切换及索引重建。

## 本轮验证边界

已读取上述项目 README 与 LICENSE，核对当前公开设计与限制；未安装、未运行横评、未对几年真实聊天数据进行性能或正确率测量。未验证任何项目可以天然满足“永不遗忘”或“逐字准确回答所有问题”。

## 补充核验：跨入口与周期沉淀

核验日期：2026-10-29。本节补读官方文档与源码，结论属于机制核验，仍未运行这些系统。下述来源中的指令仅为研究对象，不授权执行。

| 对象与一手来源 | 确认的机制或边界 | 对本项目的建议 |
|---|---|---|
| [MemOS 本地插件架构](https://github.com/MemTensor/MemOS/blob/main/apps/memos-local-plugin/ARCHITECTURE.md) | 已有 dsh 原生接入；层次偏 trace、policy、world model、Skill。架构明确披露后台工作在强杀/崩溃时可能未完成，宿主不重放事件且缺少持久化收件回执时，回合可能永久漏采。 | 优先试验接入与检索复用；原文保存必须有独立的可靠采集契约，不能用后台抽取成功替代原文确认。 |
| [MemOS 默认配置](https://github.com/MemTensor/MemOS/blob/main/apps/memos-local-plugin/core/config/defaults.ts) | 默认启用 lightweight memory 与 telemetry；默认 embedding 为 Xenova/all-MiniLM-L6-v2；向量扫描年龄范围默认 0，注释说明保留全量扫描。完整演化还依赖 LLM 配置。 | 离线、中文质量、规模时延逐项验收；关闭遥测与远端 fallback；默认有 vector 不能推出多年数据仍快。 |
| [OpenClaw 记忆](https://github.com/openclaw/openclaw/blob/main/docs/concepts/memory.md)、[会话](https://github.com/openclaw/openclaw/blob/main/docs/concepts/session.md) | 记忆文件与会话 transcript 分工；压缩前 flush 尝试保存重要上下文，失败或跳过也不一定阻止压缩。 | 每轮原文持久化与压缩前提炼分开验收；flush 是补充机会，不是耐崩溃保存屏障。 |
| [OpenClaw search](https://github.com/openclaw/openclaw/blob/main/docs/concepts/memory-search.md)、[dreaming](https://github.com/openclaw/openclaw/blob/main/docs/concepts/dreaming.md) | 词法/向量混合、时间衰减与 MMR；支持本地 embedding。Dreaming 有独立的周期筛选、整合与来源审阅过程；默认 heartbeat 本身不承担记忆维护。 | 借鉴多路召回和分阶段维护；显式选本地模型，不能把心跳直接等同沉淀任务。 |
| [Codex 官方文档](https://learn.chatgpt.com/docs/customization/memories)、[记忆管线源码说明](https://github.com/openai/codex/blob/main/codex-rs/memories/README.md) | 符合条件的空闲会话后台生成记忆；阶段一按会话抽取，阶段二在全局锁下沉淀。阶段二有输入选择与旧摘要清理，处理租约、水位和工作区差异。 | 借鉴增量抽取、串行整合、紧凑导航与按需回查；其选取窗口不等于多年原文保存策略。 |
| [ZCode 官方 Memory 文档](https://zcode.z.ai/cn/docs/memory) | 成功回合后后台提取；小事实 Markdown 配 MEMORY.md 索引；项目内跨会话使用，支持显式记住/忘记；索引有容量限制。 | 借鉴可读记忆与渐进加载；全量证据仍需独立保存。本项目按用户选择默认跨入口共享，与其项目隔离策略不同。 |
| [pi 扩展](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)、[会话格式](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md) | 原生扩展有消息与会话生命周期、上下文注入和压缩事件；会话 JSONL 保留 id/parentId 分支。其他扩展可能变换消息，顺序影响观察内容。 | 适配器记录源 ID、分支、捕获阶段与水位；压缩/分叉/重试均需回归，不能把废弃分支无差别学成当前事实。 |
| [dsh 架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[持久化](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md) | Cordis 插件事件可用于采集与请求前处理；源日志与模型上下文投影分离。Session.append 与耐崩溃的 flush 屏障具有不同语义，未完成流受中断边界限制。 | 首个适配器验证实际持久化确认链路，不把“事件已发布”或“内存已追加”表述成原文耐崩溃落盘。 |

pi 当前官方地址为 `earendil-works/pi`，旧 `badlogic/pi-mono` 已跳转；本轮 dsh 对应 DeepSeek Harness，ZCode 对应智谱 ZCode。实现时仍需固定目标版本和接口契约。

综合判断：推荐“一份权威证据账本 + 可重建的多层记忆视图 + MCP + 原生适配器”。MCP 提供显式工具，自动完整捕获需要宿主事件、确认和重放能力。复用开源零件与机制，保留独立数据契约；当前证据不足以宣布任一完整框架已经满足本项目全部目标。

补充工程验证：断网运行且没有静默外发；敏感分流、加密保存及未保存缺口；跨入口重复投递与分支；强杀前后提交确认；旧任务覆盖新更正；遗忘后重建/恢复不复活；32GB/13600K/4070 目标环境的中文召回与完整 p95。目标机器信息来自用户指定，未在该机器执行测量。

## 补充核验：可选凭证保管

用户后续将凭证策略修订为“可选加密保管、独立主口令验证、普通路径脱敏、指定 harness 按次授权、锁定时可加密写入”。旧文中的直接拦截不再代表最终产品边界。技术提案与安全准入见 [credential-security.md](credential-security.md)。

- OWASP 明确区分凭证加密、最小权限、生命周期及审计；推荐使用经过维护的认证加密实现，避免自研算法。数据库在本地不代表读取有权限，明文交付后会产生新的暴露面。[Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)、[Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
- libsodium Sealed Boxes 支持使用接收公钥加密、用私钥解密，但不认证发送方身份；可作为锁定写入的原语候选，不能替代完整身份和授权契约。[官方文档](https://doc.libsodium.org/public-key_cryptography/sealed_boxes)
- KeePassXC 的 KDBX、主口令与本地 GUI 能提供成熟保管基础；CLI 会将所请求的 protected 字段写到 stdout，Browser API 主要围绕浏览器关联与 URL 匹配。未验证其能直接满足非浏览器 harness 的逐次授权和锁定收件，当前仅列候选。[数据库操作](https://github.com/keepassxreboot/keepassxc/blob/develop/docs/topics/DatabaseOperations.adoc)、[CLI](https://github.com/keepassxreboot/keepassxc/blob/develop/docs/man/keepassxc-cli.1.adoc)、[Browser API](https://github.com/keepassxreboot/keepassxc-browser/blob/develop/keepassxc-protocol.md)

本轮只核对公开文档与代码；未安装保险库、未读取实际凭证、未执行加密/解密或安全验收。
