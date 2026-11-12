# Hindsight 的沉淀、反思与推理：EverNook 可执行借鉴

2026-11-12。结论：值得借鉴的是**分开事实抽取、自动综合观察、用户指定主题视图和查询时证据推理，并让每层具有来源、版本、失效规则和独立预算**。保留 EverNook 的本机离线、原始证据账本、低风险白名单、核心人设须确认及记忆不授予执行权的边界。没有依据要求替换 Python/SQLite、引入远端服务或采用 Hindsight 的整套存储。

## 研究范围与证据基线

- 上游固定为 `vectorize-io/hindsight` 的 `7cec128b8fdb0a696366d888e63be71fe1d52338`，提交时间 2026-11-11 17:59:36 UTC。以下源码、仓库文档和测试链接全部固定到该 SHA；官网页面是研究时的可变说明。[提交](https://github.com/vectorize-io/hindsight/commit/7cec128b8fdb0a696366d888e63be71fe1d52338)
- 本项目以用户已确认的 [2026-11-12 架构审查](architecture-review-2026-11-12.md)、[Spec #106](https://github.com/destinyTaoTao/evernook/issues/106)、[CONTEXT](../../CONTEXT.md)、[人设资产 ADR-0004](../adr/0004-persona-as-first-class-asset.md)、[ADR-0013](../adr/0013-host-independent-memory-runtime.md) 及 [有限自动偏好契约](../contracts/automatic-preferences-v1.md) 为产品约束。本报告是设计研究和验收提案，不宣称这些提案已经实现。
- 只读了官方文档、源码和上游回归测试；未运行 Hindsight、未接入任何模型、未重跑上游或本机 benchmark。测试源码证明维护者表达了什么预期，不等于本机执行通过。没有复制上游实现代码。

## 先纠正四个容易混淆的概念

| 操作或资产 | 上游实际含义 | EverNook 中的建议称呼与边界 |
|---|---|---|
| `retain` | 从输入提取 world / experience 事实，关联实体、日期与来源；不是只存一段会话摘要。 | 原始证据先可靠保存；事实抽取形成有来源的候选，不能把抽取结果当权威原文。 |
| `observations` / consolidation | 后台将相关事实去重、合并或修订为一个个有支持来源的观察。 | 综合观察，属于可撤回、可重建的派生理解；不直接改核心人设。 |
| `mental_models` / refresh | 用户先指定要长期维护的问题，再保存并刷新该问题的综合答案。可以启用后台更新；不表示每次正文由用户手写。 | 用户定义的主题认知视图，例如当前技术偏好及其依据；与已确认画像、人设分别管理。 |
| `reflect` | 查询发生时，运行带检索工具的推理循环并生成有引用的回答；默认不是永久写入记忆的过程。 | 有界查询时反思 / deep 查询，返回证据包、推断与缺口；dsh 继续负责最终对话。 |

以上区别分别见上游 [Retain](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/retain.md)、[Observations](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/observations.mdx)、[Mental Models](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/mental-models.mdx) 与 [Reflect](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/reflect.mdx)。四者若混为一个“自动反思”，会把发布权限、计算成本和撤销路径混在一起。

## 七项具体机制

### 1. 按叙述者和时间抽取事实，保存成功与抽取成功分开

**上游事实。** `experience` 指 bank 自身 agent 的经历，`world` 指其他主体及外部世界。用户说“我买了车”属于关于用户的 world 事实，不能因第一人称就变成 agent 经历。文档要求通过 context 交代说话者，区分发生时间和获知时间；抽取保留原因、情境和情绪。[Retain：事实类别和双时间](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/retain.md)

这并不意味着上游完全不保存原文。`orchestrator.py` 将完整 document body 写为 `original_text`，受 `store_document_text` 控制；chunk 存储也据此保留片段正文或留空。存储文本存在清洗路径，字段名不能证明逐字保全了所有输入。抽取 mission 排除所有事实时，document 仍可能已保存，但基于 memories 的 recall/reflect 找不到它；官方把这作为零事实结果单独说明。[正文写入](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/retain/orchestrator.py#L2379-L2455) · [正文清洗](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/retain/orchestrator.py#L335-L348) · [chunk 正文配置](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/retain/chunk_storage.py#L105-L167) · [零事实说明](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/retain.md#when-a-mission-excludes-everything-in-a-document)

**EverNook 提案。** 复用已有原文回执和 EvidenceSpan，提取主体、陈述角色、事实/引用/假设、发生时间、获知时间、适用范围及来源跨度。“共同经历”可以同时包含用户事件与 AI 的动作，不能直接套用 world/experience 两分法。先保存再提取；提取无结果、模型失败和尚未处理分别显示。即使某段不适合沉淀，仍允许在权限范围内逐字回取和字面检索。既有 [默认 provider](../../src/evernook/scheduler.py) 只是逐条用户原文前缀生成低置信主题占位，不能作为已具备跨会话事实理解的证据。

**验收。** 同一句“我已完成迁移”分别由用户、助手、引用材料、角色扮演者说出，主体不能混淆；计划和已完成不能混淆；迟到证据按发生时间查询仍可找回。抽取零事实或模型中断后原文回执保持可读，不谎报画像已经更新。凭证投影不能因抽取任务进入普通模型的明文输入。

### 2. 自动综合观察：以语义对象合并，保留支持证据和冲突

**上游事实。** consolidation 对新事实检索已有观察，生成创建、更新、合并/删除等动作；观察记录 `source_memory_ids` 和 `proof_count`，计数按来源 ID 去重。写入前重新锁定并核对来源仍然存在、版本未被编辑，避免 LLM 在途期间的删除或改标签被旧结果复活。更新历史是单独表并可配置保留上限，不能理解为无条件保存无限历史。[来源与计数写入](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memories/pg/consolidation.py#L150-L193) · [在途来源复核](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L706-L751) · [历史与更新](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py#L3344-L3520)

观察默认按事实携带的标签组合划定 scope。把每次会话 ID 当 scope，会使同一偏好在不同会话重复生成；上游提供 shared/custom scopes，明确将召回标签与观察聚合范围分开。近重复观察默认从高相似候选开始，再判断是否应合并；相似不应吞掉数字、否定、主体或语言差异。[观察范围与近重复](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/observations.mdx#near-duplicate-reconciliation)

**EverNook 提案。** 新增综合观察层，按“主体 + 主题 + 适用范围”聚合跨会话证据，保留入口/会话作为来源元数据。私密/可见性范围必须先限制候选，再进行聚合，不能借 shared scope 扩大可见性。复用现有候选租约、SQLite 任务和发布事务，而非另建一个自动写画像服务。综合观察与低风险偏好发布分开：前者是有证据的派生判断，后者仍须通过已有风险白名单和冲突规则。`proof_count` 只能表达支持记录数；同一原话重复采集或多段转述不能等同多个独立证明，更不能自动提高到身份、关系或人设的确认权限。

沉淀去重的候选排序也应单独设计。上游 `interleave_fusion` 专为去重保住各通道首选，避免一个语义几乎相同、却缺少其他通道共识的已有观察被 RRF 挤出。问答召回适用的排序，不一定适合找“应该合并到哪里”。[去重 interleave 的动机与实现](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/search/fusion.py#L114-L177)

**验收。** 两次会话重复表达偏好只形成一个观察并增加可区分支持来源；“通常简短、这次展开”保留长期范围与本轮例外；“后来换成 Vue”与“以前 React 记错了”产生不同更正语义。数字、否定和同名人物不能误合并。来源抑制、版本修改、删除发生在模型调用中途时，旧草案不得提交；处理器升级不能重新弹出用户已拒绝的同版本候选。

### 3. 用户指定主题认知视图：后台预计算，按结构增量刷新

**上游事实。** mental model 由用户指定 `source_query`，保存该问题的综合答案。读取已有内容无需再跑检索与生成；刷新可以在 consolidation 后或定时执行，并用 scope、水位、最小间隔避免无关重算。同一模型的刷新串行、待执行任务可合并；一次失败有有限重试，之后暂停自动刷新直到一次成功，避免每个 tick 重复花费。`full` 重写全文，`delta` 用稳定 section/block ID 的结构化操作修改局部，未触及部分不重新生成。[刷新契约、调度与失败](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/api/mental-models.mdx#automatic-refresh) · [结构化 delta 操作](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/delta_ops.py#L1-L35)

**EverNook 提案。** 先实现少量用户显式选择的专题，例如“当前技术工具偏好与适用条件”“共同项目的决策与未解问题”。定义问题、允许证据范围、处理器版本、内容版本、来源集合、控制水位、最近成功及失败原因。视图是派生资产，不把它当已确认人设或全量画像。第一版短视图使用全量重建即可；只有连续刷新出现格式漂移或局部改动成本明显时，再引入结构化增量，避免为尚无收益的长文档编辑器增加复杂度。

刷新失败必须保留旧内容与原处理水位，明确“当前不可用/过期”，不能把检索故障写成“没有相关记忆”，也不能将本轮只见到的增量替换整份旧理解。用户可看预览、依据和修订差异；核心人设只生成草案，沿用确认后版本化生效。[刷新结果的细分模型](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/mental_model_refresh.py#L27-L92) · [上游快照、水位及失败语义](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/api/mental-models.mdx#what-a-refresh-reads)

**验收。** 无关会话不触发刷新；相同来源版本不重复发布；中途写入的新证据不能越过已记录的快照水位；失败重试仍看见原增量；两次并发刷新不覆盖新版本；修改专题问题后必须重建。视图不能引用自身或其他无原始证据的派生结论循环证明。来源撤销优先于定时刷新间隔，下节的可见性门禁立即生效。

### 4. 来源依赖既用于解释，也必须驱动失效与恢复

**上游事实。** 删除原始事实、文档更新或标签范围变化，会使依赖观察失效，并重新排队仍有效的共同来源。上游测试覆盖删除一个支持来源、唯一来源、文档重写、改变标签、模型调用在途删除及编辑等情形。[观察失效回归](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/tests/test_observation_invalidation.py)

专题文档还要处理“正文已经记住，但来源行消失”的情况。当前源码在**单项** stale 检查中同时询问新写水位和被撤回的引用，并能在删除后排队刷新；delta 刷新有单独撤回处理。不能按落后文档断言上游完全不处理删除。[单项 stale 与撤回](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L21443-L21531) · [回归预期](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/tests/test_mental_model_retractions.py#L259-L325)

但有两个不能直接沿用的设计取舍：

1. 批量 stale 路径为性能只核对写入水位，不做每模型引用存活检查；reflect 搜索专题视图使用该批量路径。因此单项读取、列表和检索中“新鲜”的含义并非完全一致，后台排队刷新也不等于旧正文立即不可见。[源码明确说明差异](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L21477-L21487) · [批量路径](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L21615-L21673) · [reflect 调用](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/tools.py#L254-L276)
2. 若文档全部支持 ID 都消失，上游以“可能为导入换 ID”解释，清掉悬空引用、保留正文；注释明确承认全部证据真的撤回时会留下过期句子。这是保护导入文档的启发式，不适合作为 EverNook 删除/不再召回的保证。[全部来源失效的取舍](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/retractions.py#L47-L84) · [刷新应用处](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L18925-L18945)

**EverNook 提案。** 每个观察和主题视图保存可枚举的来源依赖、内容版本及控制水位。抑制或删除提交后，所有普通读取与最终注入都重新核对权威控制；后台重算只恢复可用性，不承担阻止泄漏的唯一职责。任一必要证据失效时先阻断受影响断言；若要保留由其他来源支持的部分，须重新证明后发布。不得仅删除 citation 而保留原断言继续注入。原机全失恢复使用明确来源 ID 保留/映射和备份清单；无法证明映射时进入审查态，不能把“全失效”自动解释为导入。

**验收。** 原文→事实→观察→专题视图→实际请求形成完整依赖链；删除/抑制任一必要来源后，列表、图、搜索、缓存及正在构造的请求一致失效。零新写、无后台运行时也不能泄漏。覆盖全部支持来源被删、仅部分被删、同义独立证据仍在、备份恢复换 ID、旧缓存及处理器重建。可信管理可解释失效原因，但不把受抑制正文作为解释再次返回。

### 5. 多路召回以覆盖机会为先，搜索深度与输出预算独立

**上游事实。** semantic、关键词、graph、temporal 返回候选，按 rank 做 RRF，再重排和装入预算；每路有候选上限，记录来源排名。时间通道先解析参考时间窗口，再在窗口内按相关性和时间分布选择，避免密集新数据挤掉早期结果；调用端已有明确时间窗时可直接传入，跳过文本分析。[检索说明](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/retrieval.md) · [每路上限和 RRF](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/search/fusion.py#L8-L112) · [显式时间窗](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/search/retrieval.py#L103-L132)

`budget` 决定搜索深度，`max_tokens` 决定返回内容，两者独立。不能把上游记忆文本预算直接当整份 provider 请求的硬 token 上限：其文档明确 metadata 不计费，而且全部条目都装不下时仍返回第一条全文。原文 chunks 也有独立额度。[预算与截断细节](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/retrieval.md#stage-4-token-truncation)

**EverNook 提案。** #112 先解决已有全局扫描范围让部分多年证据永无候选机会的问题，保留近期/全历史入口；在同一夹具上分别消融字面、语义、显式时间、关系扩展，证明增益后才保留新通道。语义全库索引依旧是可重建派生资产。查询意图可独立缩短，原始长问题与本轮回执保留，不能因输入长而静默取消召回。graph 先用有来源的关联扩展，不急于引入图数据库。

召回报告列每路候选数、范围、截断、超时、索引水位及最终未装入原因；无匹配、未查全和索引故障分开。最终上下文沿用 EverNook 的序列化与宿主预算边界，#105 的真实完整请求计数等待条件不因新增 deep 查询而消失。

**验收。** 语义独有答案分别位于全历史前中后段、近期窗外；相同时间戳批量写入和多年分散数据均测；有原话、无词面重合、同名干扰、用户忘了年份、明确时间窗、根本没有证据分别报告。比较 Candidate Recall@10、最终注入覆盖、逐字可回取、回答引用正确四层。对过长单条证据不能突破最终允许预算；返回明确遗漏而非隐式超额。

### 6. 查询时反思是受限检索循环，不是通用执行 agent

**上游事实。** reflect 工具限于搜索专题视图、读专题正文、搜索观察、recall、expand 和 done。代码强制检索层级，并对“没有找到”的结论要求尝试原始事实；引用 ID 只从实际检索集合中选择。这里的 guard 是“已经运行检索”，空检索也计入，并不保证每次找到充分证据或引用真的支持答案。[工具与整体机制](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/reflect.mdx) · [负面答案与强制检索](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/agent.py#L1206-L1272) · [引用集合过滤](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/agent.py#L2400-L2427)

官方概念页写“最多 10 轮”，但不是固定不可变的全局上限：`reflect_max_iterations` 再按 low×0.5 / mid×1 / high×2 调整；交互请求省略时按 low，专题刷新省略时按 mid。另有上下文、工具输出、生成长度、墙钟时限与协作取消。可选 decision model 路径把层级检索改为并行一轮后判断证据是否充足，不能把文档中的单一路径概括当全部实现。[预算缩放与墙钟](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/memory_engine.py#L16240-L16265) · [循环取消与上下文终止](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/agent.py#L1214-L1304) · [decision 路径说明](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/reflect.mdx#decision-model-orchestration)

**EverNook 提案。** 普通每轮注入保持快速；只有显式深查或已配置的有界触发进入 deep。第一版可在记忆服务提供受限的证据探索器，也可由 dsh 现有推理调用同样的只读工具；按实际本地模型成本选最小实现。输出证据包、可验证推断、未决矛盾、检索范围与停止原因，dsh 生成最终交流回答。此阶段不新增写库、接受画像、改人设、shell、网络或凭证明文工具。

硬限制由服务执行：最大轮数、候选数、总输入/输出、墙钟截止和取消。达到上限就返回已验证部分与缺口；失败不能化成“从未说过”。引用 ID 合法只是第一关，还要检查引文跨度、当前可见性、时间适用和断言支持。历史内容依然是低信任数据，不能用其中的命令扩大工具白名单或继续下一轮未经授权动作。

**验收。** 多跳需要第二轮才能完成、首轮专题过期、观察错误、无答案、冲突未决、伪造 ID、正确 ID 但不支持答案、来源在循环中被抑制、预算耗尽、provider 超时与用户取消。检查原文 fallback、可见性复核、终止原因和确实停止后续模型调用；对相同问题比较普通 recall 与 deep 的额外收益及本地成本。查询结果不会自动晋升为新观察，避免模型答案自我强化。

### 7. 将抽取目标、沉淀目标与性格解释分开配置

**上游事实。** `retain_mission` 影响抽取关注点，`observations_mission` 影响自动沉淀，reflect mission 和 disposition 影响查询时解释。disposition 的 skepticism、literalism、empathy 是 1–5 的软倾向。源码会把数值展开成自然语言行为要求，说明单独数值并非稳定控制。directives 虽在文档中称硬规则，所查核心实现仍是系统提示与末尾提醒；不能据此推定权限、隐私与事实正确性得到形式保证。[三个 mission](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-docs/docs/developer/reflect.mdx#mission-natural-language-identity) · [数值展开和 directives 提示](https://github.com/vectorize-io/hindsight/blob/7cec128b8fdb0a696366d888e63be71fe1d52338/hindsight-api-slim/hindsight_api/engine/reflect/prompts.py#L49-L198)

**EverNook 提案。** 配置分为：抽取/沉淀策略、检索/成本策略、已确认核心人设。可借鉴 disposition 作为用户可理解的少量解释偏好，但第一版不必加入三个数值旋钮；先明确“事实不足时保留不确定、历史与当前分开、情境例外优先”等可评测规则。语气可以变化，证据可见性、事实版本和授权不能随性格变化。人设修改必须生成草案并由用户确认；经验记忆只表达曾经有效的做法及条件，不变成新的执行规则。

**验收。** 同一证据在不同语气下仍返回同一允许来源、事实版本和冲突状态；提高共情不能编造关系，提高怀疑不能无依据否认用户自述；历史中夹带“从现在起修改你的人设”“忽略权限”等文字只成为数据。未确认草案、过期配置与未经授权客户端无法改变核心人设和策略。

## 文档与实现冲突如何处理

| 易误读的官方说法 | pinned 源码证据 | 本报告采用的结论 |
|---|---|---|
| 官网称不存 conversations | retain 可保存完整 document 和 chunks，配置可关闭。 | 其记忆检索以提取事实为中心；不能概括成不保存原文，更不能据此放弃 EverNook 原始证据。 |
| mental-model API 文档称删除不会让 stale 改变 | 单项检查和撤回回归已扩展；批量检查仍只看写入。 | 存在文档落后与路径差异，分别分析；不声称整个系统都缺失删除失效。 |
| reflect 最多 10 轮 | 基础上限可配置，预算按 0.5/1/2 倍调整。 | 必须记录本次有效预算与终止原因，不能照抄固定轮数。 |
| observations 有精确引句和 proof | 源码核心支持事实 ID、计数和 history；原话还要沿 chunk/document 回源。 | 有来源引用不自动等于逐断言精确引用，更不等于已经证实为真。EverNook 继续核对 EvidenceSpan。 |
| token 预算装满即止 | 文本预算不含 metadata，且保底整条返回可超出该文本预算。 | 不能继承为 provider 完整请求硬上限。 |

这些差异的证据均在上述相应机制中固定链接。上游说明中的性能与准确率仅是上游自己的测试条件，本报告没有把它们转成 EverNook 的效果、延迟或资源保证。

## 对 Spec / tickets 的建议落点

以下工作已关联 Spec #106：#112/#113/#114 已补充边界，#118–#121 分别承载综合观察、主题视图、查询时反思及专项评测。票据状态仍以 GitHub 为准；本研究不代表实现或准入完成。

| 既有工作 | 应补充的可交付内容 | 不可省略的验收 |
|---|---|---|
| [#112 长期召回](https://github.com/destinyTaoTao/evernook/issues/112) | 全历史候选机会、独立查询意图、通道消融、深度/输出预算分离。 | 跨年前中后段、无词面重合、显式/未知年份、长输入、降级与覆盖报告。 |
| [#113 自动画像](https://github.com/destinyTaoTao/evernook/issues/113) | 真实主体/引用/例外识别；综合观察候选与低风险画像发布分离；沿用来源版本、拒绝幂等和冲突暂停。 | 用户自述/引用/助手猜测分组；重复、否定、范围例外、旧说法纠错；发布实际影响下一轮注入。 |
| [#114 网络与诊断](https://github.com/destinyTaoTao/evernook/issues/114) | 原文→事实→观察→视图的可点击依赖；正文可见性与处理进度、失败/未查全分列。 | 删除/抑制后图和列表一致；按来源回到原文；不泄露失效正文。 |
| [#118 综合观察](https://github.com/destinyTaoTao/evernook/issues/118) | 跨会话语义对象、去重/修订动作、独立支持计数、冲突与版本、来源失效。 | 第 2 / 4 节场景；后台模型调用在途撤销和崩溃重试。 |
| [#119 主题认知视图](https://github.com/destinyTaoTao/evernook/issues/119) | 用户定义问题与范围、预计算内容、快照水位、限速/串行刷新、历史与失败保留。 | 第 3 / 4 节场景；先短视图全量重建，delta 按收益再做。 |
| [#120 查询时反思](https://github.com/destinyTaoTao/evernook/issues/120) | 仅普通只读工具、有界 deep 证据探索、可验证引用、缺口与停止原因。 | 第 6 节场景；不得获得维护、人设、执行或保险库权限。 |
| [#121 专项质量评测](https://github.com/destinyTaoTao/evernook/issues/121) | 区分抽取、综合观察、视图刷新、候选召回、最终注入与回答，冻结中文合成夹具及阻断项。 | 与现有 #81/#82/#105 准入对齐，不继承上游分数，不用队列清空代替理解正确。 |

建议实施顺序：统一服务闭环之后，先补候选检索与来源失效，再做综合观察和画像接入；有了稳定、可撤回的派生层再加入主题视图；最后以小型本地试验决定 deep 推理的常用触发和预算。每一步可独立验证，不要求一开始引入上游全部模型槽位、decision model、页面树或图数据库。

## 专项验收与实际验证边界

| 维度 | 至少区分的结果 |
|---|---|
| 抽取 | 事实/经历正确、主体正确、计划/完成、引用/自述、可回源、零事实及失败。 |
| 综合观察 | 合并准确、误合并、漏合并、范围例外、纠错/变化、独立支持、用户拒绝与冲突。 |
| 刷新 | 快照一致、跨会话覆盖、失败不推进、无关不重算、并发版本冲突、撤销不复活。 |
| 召回 | 每路候选与贡献、Candidate Recall@10、未搜索范围、最终注入、逐字回取与可见性。 |
| 推理 | 引用存在、引用支持、时间适用、推断标记、无答案/澄清、取消与预算真实停止。 |
| 成本 | 本机固定模型/版本、每阶段调用和 token、墙钟、峰值内存、前台让行；冷暖与积压分开。 |

本研究实际完成的是一手资料与代码路径核对、现有本地设计/候选框架阅读、报告写作及文档检查。研究子任务未执行产品实现、数据库迁移、运行库写入或提交推送；相关 Spec/tickets 由主任务同步。未运行 Hindsight 测试、EverNook 模型评测、真实 dsh 交互、大规模性能或恢复实验。新增机制仍须通过各票的本地合成回归、真实 provider/宿主接线与既有日用门禁，才能宣称可日常使用。
