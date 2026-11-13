# 长期记忆学习闭环实施计划

> For agentic workers, use superpowers:executing-plans.

**Goal** 按#121→#112→#113→#118→#119→#120交付，并逐票验证提交推送。

**Architecture** 沿用本地证据账本、任务、来源控制和独立服务。新评测使用独立冻结套件，复用既有有界JSON读取、规范化与哈希工具。原始观察、缺失和失败独立保留。

**Tech Stack** Python3.14、SQLite、现有本地模型接口与TypeScript适配器。

**Spec** GitHub#106及#121/#112/#113/#118/#119/#120。

## Global Constraints

运行数据仅在checkout外；只用合成语料；旧zh-memory-v1与#81policy冻结身份保持。普通召回、深查预算独立。模型或派生功能未运行时报告missing。用户授权常规实施及逐票提交推送。

## Review Focus

重复或错误来源增分、错误quote但合法ID、过时/抑制/删除来源、未测字段伪装零、不同模型/策略结果混合，全部进入#121负向回归。

## Task 1 — #121

Files
- 新建eval/zh-learning-v1/{corpus,expectations,manifest}.json及README.md。
- 新建src/evernook/learning_evaluation.py，load_suite(root)、score(suite, observations)和validate/score CLI。复用evaluation的严格读取和规范化。报告四个arm、阶段和各维度passed/failed/missing、Recall@10、引文校验、规范决定、测量及身份，admission保持pending。
- 新建probes/admission/learning.py，通过公开API在新合成运行库采集raw arm的真实召回/引文与现有偏好处理观察，写逐项回执、水位、原始输出、时间和实际资源。待实现功能不补造。
- 新建tests/contracts/test_learning_evaluation.py，覆盖身份/篡改、矩阵缺失、重复/非法引用、错误引文、stage绑定、预算与观测元数据。

Interfaces
- 输入观察固定suite_hash、configuration和rows；每行绑定case_id、arm、payload及measurements，case绑定具体phase。configuration绑定模型/预算/策略版本。
- 四arm为raw/observations/views/reflect，阶段涵盖initial/replay/continued/processor_changed/restored。

- [x] 写负向测试并确认缺实现失败。
- [x] 冻结合成跨年/偏好例外/否定反例/变化纠错/多跳/未知/来源控制/旧备份/注入语料。
- [x] 实现加载和评分；运行正负控制测试及strict mypy。
- [x] 实际执行独立raw基线，保存failed/missing和测量；审核冻结前案例及评分器，记录边界。
- [x] 按#121提交推送、更新spec和issue（27dd736）。

验证：新旧评测30项通过；mypy显式package bases及MYPYPATH=src;.检查3文件通过。报告docs/reports/learning-baseline-2026-11-13.md。

Ruling: 将冻结前独立复核提前到#121提交前 — 后续各票依赖稳定金标准，等全部实现后再发现评分假阳性会污染比较 — 成本为一次独立评审；无需重复评审已由回归覆盖的修复。

Final: fixed 删除/撤销混淆、历史位置缺口、连续居住证据、宿主自身引文及Recall覆盖率 — 五组负向回归RED→GREEN，最终新旧30/30。
Final: fixed 实现身份从checkout切换实际import包 — 安装路径与字节变更回归通过。

## Task 2 — #112

基于#121的纯同义、全历史位置、长输入和控制失效红例，修改semantic.py/host_context.py及必要服务入口。明确索引候选空间和覆盖，避免固定前缀永久不可见。不为通过而改变冻结语料或门槛。按证据选择向量候选实现，验证旧索引/重建/取消/并发和真实安装入口。

实施分解（#121已冻结）：
- 添加query_plan.py，原文完整校验；显式query_intent独立短句；未提供意图的长文本只选择有边界的尾部问题并明确query_coverage_limited，不再静默丢弃全部/截取开头。贯穿Core/context/HostContext/MCP/service/dsh兼容路径。
- semantic既有BLOB全历史精确候选，撤销/来源版本/时间在topK之前过滤；取消和deadline由SQLite progress handler及Python每行检查控制。不再按接收顺序固定扫描前10000条。
- 评估后采用可选vector extra的sqlite-vec==0.1.9本机C标量cosine加速，继续查询普通semantic_chunks_v1，不引入virtual table、第二份向量或远端服务。未安装时Python全扫描；安装/加载故障可诊断降级，保留字面路径。原始库/索引结构不变，卸载extra可回Python路径，无数据回滚。
- 超过10000的前/中/后位置、未知时间、长输入显式意图、原文凭证保护、删除/抑制、换模型、旧索引、并发更正、取消/超时与native缺失回归。补C候选与Python exact相同的结果对照。
- 配置embedding的服务维护线程补小批量向量化；refresh加入全程取消/截止与提交前检查，前台/暂停/退出不发布。普通语义失败单独记录，继续独立scheduler。
- 使用冻结#121原语料实测并独立记录候选/上下文/宿主证据。10万/100万和完整轮次p95沿用#81/#84，不把小型内核或夹具时延当作准入。

选型证据：仓库外evernook-112-vector-selection的10016×384合成向量内核，旧前缀只得1/5002位置、漏10004；Python完整扫描与sqlite-vec完整扫描均得三位置。各3次调用：Python完整约239–240ms，C标量约21–22ms。不是生产ledger JOIN、真实语义质量或规模验收。官方资料sqlite-vec Python与KNN scalar文档已核对，Context7本月额度耗尽后使用一手网站。新可选依赖的必要性/影响/回退如上，属于用户已授权的召回选型范围。

实际10004来源/MiniLM全部建索引后，旧前缀同库漏末位、新exact可达末位；中位查询相似度.355低于既有.4，两者均漏。保留原始失败，随后补复用现有ready FTS的中文双字/英文词OR候选，先按来源去重再取100来源进入RRF，不改语义阈值。该新增组件单独独立复核，重复FTS行挤占预算/自增分问题已有RED→GREEN回归；每通道每来源只计一票。--lexical-ready是新独立策略观察，不补写旧基线。

- [x] RED→GREEN：全历史/长输入/保护/控制/取消回归。
- [x] 各入口接线及类型/相关契约验证。
- [x] 实测冻结对照、独立评审、逐票提交推送与tracker实况（20512b2）。

Ruling: 选择现有BLOB的C/Python精确搜索，不新增ANN索引 — 位置内核已证明固定前缀漏检，C标量降低小样距离计算成本并沿用现有控制/备份/删除语义 — 代价是全历史线性成本，百万规模p95仍可能需要后续索引选型，不能颁发日常准入。

Final: fixed #112独立复核的直连MCP取消、尾部空白、refresh提交前取消及语义错误阻断scheduler — 可复现问题分别有回归覆盖；新关键词重复行问题亦RED→GREEN，最终相关167 tests passed，strict mypy src/tests 122文件通过。
验证：源码dsh服务4场景及长输入双预算场景通过；实际Core0.1.16/preview.23/固定打补丁rc.5宿主4场景通过。最终wheel53个Core代码/worker文件与源码/安装逐字节hash匹配，npm37文件匹配；安装服务无宿主时真实MiniLM向量化及长输入意图通过。10004来源真实位置全部索引，最终三位置3/3，原prefix仅1/3；小套件失败/缺失及质量/规模门禁保留。见docs/reports/long-term-recall-2026-11-13.md。

## Task 3 — #113

依据冻结偏好、临时指令、否定、引用和冲突案例扩展preferences.py/automatic_preferences.py，复用候选及原子发布。来源可回溯，逐类准入，阶段统计明确，实际宿主下一轮使用新值。高风险保留确认。

实施分解：
- 新建preference_statements.py作为有限规则的整段识别/跨度/范围入口；保留Qwen保守整段候选路径。确定性规则从完整原文识别可证明的第一人称低风险子句，保留长期/本轮/否定及引述/条件/不明语境的区别，禁止只拿正向子串绕过否定引用。
- 规则候选及可信发布复用同一个完整原文解析器验证精确跨度/类别；处理器身份包含新规则字节，新版本不继承旧准入。临时例外不发布且不反复进入晋升队列；冲突保留双方证据、暂停旧标签，明确纠错/否定仍遵守可信策略。
- scheduler识别长段落中的合格候选，提取/草案/自动发布/澄清/忽略分别统计，不把“任务完成”当作“画像已更新”。
- 跨会话经历复用既有quote-only ExtractionProcessor/CandidateWorkflow、导航及审查工作流，补必要的来源角色/发生与获知时间/去重身份投影与可重建候选入口；不把抽取式摘录当作模型归纳或事实准入。#118的综合观察单列。
- 用#121长期/例外/否定/引用/冲突的固定分项、当前旧偏好回归、来源抑制/更正/旧备份、processor换版及下一轮实际dsh provider验证；保留失败/missing，逐类准入不扩大高风险或persona权限。

- [x] 解析与发布边界负向回归RED→GREEN。
- [x] 跨会话候选来源/时态/阶段契约及维护接线。
- [x] 冻结实跑、实际宿主下一轮、独立复核；提交推送与GitHub同步随本票提交。

Ruling: 旧“完整句排除”测试中的“否定观鸟、肯定摄影”改为两个独立爱好分项 — 用户已要求完整长段落中分别识别偏好与否定，二者不是同一槽的冲突 — 成本是有限规则需要更强整段上下文保护；原引用、不确定、高风险、身份/财务/病史、权限指令等排除断言均保留，另加同段同槽冲突及直接发布复核。

明确否定纠错采用既有dispute+profile draft暂停旧值，报告withdrawn；不执行原始记录更正/抑制，不把自然语言句子变成可信UI的suppress批准。普通相反陈述仍走澄清，手工确认/拒绝/抑制的保护不扩大。

Final: fixed #113独立复核四项：逗号前临时限定、未解析否定、撤回对象误配及历史段落草案冲突检查，RED→GREEN并独立重跑4/4。源码联合首次103/104，未定位重启退出保留；同项3次和最终服务6项通过，其他98项已通过，安装19项、源码及安装实际dsh下一轮各1项通过，mypy126文件及probe、TS typecheck通过。冻结automatic27/0/6，其余质量/规模/missing不隐去。报告docs/reports/automatic-profile-2026-11-13.md。

## 后续任务

#118综合观察、#119主题视图、#120显式反思分别补充具体实施步骤，复用已冻结分项和控制失效契约。

## Task 4 — #118

复用SQLite权威来源检查与持久候选租约，增加独立ObservationRegistry。观察是派生理解，不等于用户自述或已确认画像。默认生成候选；后台仅对已识别有限偏好按确定性槽聚合原始依据。自由归纳需可信review确认，不能用频次/置信度或来源中的指令提升权限。

Files: 新建src/evernook/observations.py（事件/来源/增量决定及只读投影）、observation_processing.py（有界任务/规则构建）；tests/contracts/test_observations.py；cli.py/service.py/service_maintenance.py提供可信控制与独立进程维护；purge.py/archive.py/runtime_restore.py传播删除与恢复控制。新表均为可重建派生资产，默认不启用自动处理，停用不影响原始记录。

Interfaces: ObservationDraft(topic_key,conclusion,support,counterevidence,category,valid_from,valid_until,processor_version,policy_version,inference)；propose(access,request_id,draft,expected_revision=0,operation='create',observation_id=None,parents=())形成候选；decide(access,request_id,observation_id,expected_revision,action,confirmation_id)可信发布/撤回；read返回当前状态和完整原始依据角色/发生及获知时间；related按结构化槽及独立词面相关性查找，不使用问答RRF， exact slot始终优先。来源跨度去重，重复原始event、候选及摘要不增加独立证据数。

1. [x] RED→GREEN：来源/span/版本、反例不丢弃、重复与摘要回灌；create/merge/narrow/split/change/correction/withdraw版本语义；角色/有效及获知时间；风险确认。
2. [x] RED→GREEN：来源失效时正文阻断、提交前撤销与取消回滚、持久幂等任务/租约崩溃重试、换版不复用旧任务结果；去重查找不被问答融合挤出。
3. [x] 独立服务可选后台维护与可信CLI闭环；普通读取不获得维护权限；删除彻底清理派生正文、旧备份需当前来源重新生成与ID重映射，不假设丢ID可继续用。
4. [x] 冻结套件独立observations arm候选审计、分项/对照、独立复核及必要相关验证；文档、逐票提交推送、GitHub同步随本票提交。未运行模型或尚缺分项仍保留missing和门禁。

Ruling: 默认采用有限偏好review候选，其他有来源归纳沿可信审查 — 当前本地模型尚未质量准入，不能用一个通用自由生成入口代替事实安全和证据验证 — 成本是非偏好及语义推断仍需后续模型准入，候选层独立替代原始召回的冻结检索只有6/21/6，未声称学习质量已通过。
Final: fixed 聚合删除任务不一致、64跨度溢出永久重试、32候选预算不一致及overflow split遗漏晚反例 — 四组RED→GREEN，独立复跑3+1通过。相关87项、最终观察22项、独立安装24项及安装租约/换版2项通过，mypy129+probe通过。冻结候选审计12/42/738，失败和missing保留；报告docs/reports/observations-2026-11-13.md。

## Task 5 — #119

新增topic_views.py负责用户定义、持久合并刷新任务、只读快照与来源/水位验证；topic_view_refresh.py复用SemanticRecall和已发布ObservationRegistry构造有界原始摘录/已审观察视图，不加入新的模型或生成权。默认不配置视图。定义包含title/purpose/query、来源scope、观察topic keys、TTL、策略/版本；状态区分empty/pending/valid/expired/conflict/paused/deleted/needs_refresh。自由推理留#120。

首次配置视图时安装可选SQLite单行递增水位触发器：原始新增evidence_epoch、来源更正/抑制/删除control_epoch及观察更新derived_epoch分开。与原始事务原子更新，失败不回“已保存”；不按每条消息写N个视图任务。后台按view_id合并最新目标水位、有界/轮转刷新，提交前复核定义、来源及三类水位；有新消息或撤销则旧正文不可用，即使尚未后台刷新。最初全库保守失效，不承诺无关更新可保持所有视图有效。

Interfaces: TopicViewDefinition(title,purpose,query,entries,sessions,source_ids,observation_keys,since,until,ttl_seconds,policy_version)；TopicViews.configure(access,request_id,view_id,expected_revision,definition)、control(...action pause/resume/delete)、read(access,view_id)、refresh(...timeout,cancel)、tick(...)、add_evidence(...)显式用户编辑另存原始user事件。query/context仅读当前可用快照；已确认人设独立。ContextPolicy新增topic_view_ids与topic_view_tokens，HostContext读取配置并复用总预算及最终版本/水位复核；服务CLI与维护共享。

- [x] RED→GREEN定义修订/暂停/删除、TTL与新输入/撤回两类水位、来源更正/抑制/删除、策略换版；刷新提交并发和幂等。
- [x] 服务有界后台任务/租约恢复、合并大量更新、显式用户编辑留原始证据、旧快照拒绝及导入后重新刷新；scope与普通身份权限。
- [x] 有预算普通上下文接入及最终复核，实际dsh请求采用有效视图、源失效时旧正文不再注入；冻结views分项、独立复核、文档、提交推送及GitHub同步随本票提交。

Ruling: 初版主题视图由原始摘录与已审观察组成，使用分开的持久证据/控制水位保守失效 — #118候选单独检索6/21/6不能替代raw，先保证定义/引用/撤回/刷新闭环，再由#120提供显式推理 — 成本是无关新输入可能使视图待刷新，触发器写入成本需实测而不能继承#84准入。

Final: fixed跨操作幂等指纹、传递处理器版本、显式编辑scope、真实transport嵌套结果及分词期撤回；两轮独立指定范围复核通过。最终联合115项、安装28项、固定rc.5实际dsh1场景、mypy134+probe及TS通过。views检索21/6/6、引用19/8/6未优于raw，保留失败/missing；未定位ResourceWarning和此前隔离复跑通过的维护/文件锁失败。见docs/reports/topic-views-2026-11-13.md。

## Task 6 — #120

新增reflection.py负责显式只读探索和预算、reflection_model.py/reflection_worker.mjs负责固定本地Qwen实验进程和真实token计量。复用既有模型文件hash/离线运行，不新增依赖、不继承抽取用途质量准入。服务启动可选--reflection-config，可信reflect-configure显式安装既有三水位机制；查询本身不建派生资产、不写画像/事实/人设。普通召回不会触发模型。

Interfaces: ReflectionBudget(seconds,search_rounds,model_rounds,tool_calls,candidates,sources,source_chars,input_tokens,output_tokens)；ReflectionPolicy(normal,deep,concurrency,cache_entries,cache_ttl)分别配置；ReflectionEngine.reflect(access,query,mode='deep',topic_view_ids=(),timeout=None,cancel=None)。私密/凭证信号在任何向量和模型调用前分流。模型只能返回严格search/read/navigation/view/done动作，动作参数二次验证；不传入维护身份或通用工具dispatcher。导航只沿已发现普通来源的session邻近事件探索；每次工具前重新授权，原始回源和最后交付均验证span/版本/全局时钟/选中视图。

结果含原始精确引用、模型综合（始终unverified inference，不以引用存在证明蕴涵）、冲突、未知/澄清、覆盖/停止原因和实际预算计量。模型无证据结论拒绝；回源限额与候选额分开；无答案要保留raw探索是否完成。cache默认关闭，可选仅进程内、绑定身份/输入/模式/策略/实际处理器及证据控制水位，返回前仍复核，绝不把结果自动回灌长期记忆。超时返回可验证证据与空推断，取消杀worker且不再调用模型。

- [x] RED→GREEN：两轮关联、未知弃答、冲突、伪造来源/跨度、prompt injection越权、敏感预分流；时钟/来源在model/token/return期间变化及cache撤销。这里是合成backend机制测试，不是模型质量。
- [x] 独立normal/deep预算、工具/候选/回源/模型轮次和token独立上限、并发、时间、取消资源回收；服务端配置可信、MCP/dsh只读闭环。
- [x] 实际固定Qwen有限案例和冻结reflect arm独立记录；必要相关测试/独立复核、文档，默认关闭的已验证功能基础提交推送/GitHub同步随本次提交。不改gold/policy或把synthetic backend当模型质量。
- [ ] 真实模型用途质量逐项通过；目前6例judgment1/5/27、存在模板复读，#120保持open，不发布日用反思准入。

Ruling: 反思默认临时、模型综合标为未验证推断，只有精确原始引用通过机械校验 — 小型模型尚未用途准入且引用合法不等于语义支持 — 代价是仍需冻结模型评测决定是否能日常启用，服务配置与实验通过不能替代质量成绩。

Ruling: 原始工具审计保留但结构化反思产物不能再作为普通记忆依据 — 用户同时要求保留tool原始记录与避免模型自增信，删除审计或把生成内容当用户原话均不合适 — 成本是普通召回增加captures主键过滤，任意未标注助手改写仍只能按非独立角色处理，性能/更广血缘不在本次声称内。

Final: fixed最终unknown/coverage旧正文残留、MCP默认提前超时及105条反思tool_result回流挤占，均RED→GREEN并独立复核。功能组102项、增量组113项、安装41项、rc.5实际dsh1场景、mypy139+probe及TS通过；实际Qwen取消worker剩余0。冻结reflect11/7/774、judgment1/5/27，三条合法done只复读模板，不关闭#120质量验收。见docs/reports/reflection-2026-11-13.md。

## Task 7 — #120 模板复读与证据支持质量

继续内联执行已授权的质量修复。基线86e79cc；固定Qwen/量化、MiniLM、原冻结suite/scorer/policy、预算及只读/私密/来源/取消门禁均保留。先建立模型输入→原始action JSON→解析→引用校验的合成数据诊断，不从gold生成答案，不启用日用。

- [x] 重现模板与引用错误：无证据及跨会话两条证据均生成提示示例的完整done/占位text/e1。原始worker输出已在仓库外保留；拒绝发生于真实引用校验，未发现解析器改写正确答案。communicate收尾会重复暴露同一output，后续trace按worker去重。
- [x] 外部单变量提示对照：移除假答案/假ID，确认单改提示不足后采用短协议/独立示例，保留真实当前引用映射。进一步复现canonical排序及提前EOS；不放宽引用或替换gold。
- [x] 回归并修复：提示/演示隔离、严格JSON/引用、精确原话绑定及绝对跨度、可选assessment兼容与最终失效清除；格式新生成消耗旧额度，未解析后补字符。未知年份/有限偏好冲突分别标规则保护，不冒称一般语义验证。
- [x] 同一冻结六例对照及八例有界扩展；保留失败、真实wire、来源/判断/时延/missing。paragraph后用于字段顺序定位，不将扩展样本冒称独立holdout；模型用途准入仍不满足，#120保持open。
- [x] 联合105项、最终增量36项/安装58项、mypy142+probe/Node检查、独立33项及追加2项复核、实际rc.5 dsh场景与真实取消worker剩余0；67代码/worker成员字节匹配。验证通过的修复提交推送，GitHub#120/#106与报告同步随本次提交。

Ruling: 优先在现有固定本地模型上分离提示与协议问题，不换模型或加云回退 — 已确认提示自身提供了被复读的假答案及e1，先排除可修复输入污染才有依据评价模型能力 — 成本是可能仍暴露0.6B能力上限；冻结失败不得因此被删除。

Ruling: 模型示例保留action/value/support顺序，canonical JSON仍用于用户packet和fingerprint — 相同输入仅恢复演示顺序即可修复paragraph遗漏value，文本顺序对小模型有实际影响 — 成本是仍需按用途评价，不能以JSON语义等价推定生成行为等价。

Ruling: 不补JSON或猜引文，以生成中抑制EOS/完整对象停止并保留原硬预算；明确未获原话年份支持的回答保守弃答，分别标规则来源 — 实际wire证明EOS未闭合及元数据被误当事件年份 — 成本是相对历法推算暂不支持，不能把guard成绩算成模型理解。

Final: 原6例judgment1→3通过（其中两例为年份/偏好规则保护）；扩展8例4/4/25，协议8/8完整，模板句0。multi仍错目标属性，injection/authority仍有错误语义判断，old仍未找回原始依据；#120及日用门禁保持open。根因与最终证据见docs/reports/reflection-quality-2026-11-13.md。

## Task 8 — #120 多跳与来源语义，不用规则替代模型

基线295d6ea/Core0.1.21。用户明确要求不新增规则掩盖能力不足：既有规则保持独立标记，本轮模型语义成绩只统计未经规则改判的原始模型决策。原zh-learning-v1/scorer/policy和只读/私密/引用/水位/预算/取消边界保留，不改原验收门槛。

- [x] 先冻结独立补充诊断zh-reflection-semantics-v1（10个provided-evidence合成对照，cases hash eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0）：相同事实换目标属性、工具指令/工具事实、助手猜测/用户确认/查询助手当时说什么。不是原套件替代品、检索或宿主答案准入；expected不得进入模型packet。
- [x] 重现三类失败，完成独立演示、输入呈现、官方采样、原生thinking和独立固定1.7B资产的仓库外对照；各次控制变量/失败保留，不靠always-abstain提高分数。1.7B原型未接入生产配置且超过资源目标。
- [ ] 对有证据改善的最小模型方案接入：本轮未找到，保留为#120后续工作，不把诊断交付当语义修复。已经实现原始模型动作、规则改判和最终输出分层记录；原话支持不冒称蕴涵验证。
- [x] 补充集当前基线与四示例均严格5/10，采样3/10、1.7B为1/10；原冻结8例安装实测仍judgment4/4/25，其中两例为规则保护。thinking三重点例均30秒超时，不提高预算。最终采集器完整十例复验，输入/hash留存。
- [x] 相关35项、strict mypy143文件及新probe/测试2文件、独立代码与指定结果复核通过。67个Core/worker成员与既有非editable安装一致；本轮无生产改动，不重建安装或重复旧服务/dsh/取消接线验收。诊断增量提交推送并同步#120/#106，保留用户已有工作区改动。

Ruling: 用模型原始决策和角色反事实对照检验能力，禁止新增来源类型一刀切或问题关键词规则代答 — 失败发生在正确证据已给到的模型侧，引用/权限防线不等于理解能力 — 成本是可能必须承认当前0.6B能力上限，不能用规则成绩换取关闭本票。

Ruling: 不合入任何本轮模型/提示候选 — 严格质量均未改善至可接入，四示例还暴露来源顺序导致ID对调，1.7B大多无理由弃答且采样峰值5.09GiB — 代价是#120继续开放；本轮只交付经验证的诊断基础和真实失败记录，后续须按模型用途及资源重新准入。

Final: 原冻结suite/scorer/policy、生产协议和权限边界保持不变，未启用用户profile。见docs/reports/reflection-semantics-2026-11-13.md。

## Task 9 — #120 本地模型/后端候选，先评测再接入

基线0d3eef9，用户授权不再叠规则/提示模板。沿用既有冻结10例、原学习suite/scorer/policy、build_messages及原有规则标记；候选运行只在仓库外使用合成资料。按writing-plans内联执行，常规实现不另确认。

- [x] 首批固定Qwen3-1.7B GGUF、Qwen2.5-1.5B-Instruct、Qwen3.5-2B；后按下方ruling增加4B低比特及原2B的Q6控制。固定llama.cpp b12360 CPU，无新监听/云回退；仓库外资产验证、量化来源与文件摘要保留，不增加生产依赖或下载器。
- [x] 事前固定筛选标准：完整10例严格raw至少8例、四负例错误肯定0、六个可回答至少5例；原话/输入/输出/30秒/4线程不放松，private bytes与working set均记录，2GiB资源及原日用门禁不改。
- [x] 新增仅admission的local_reflection/native_completion/native_chat，固定资产清单，纯生成文本复用原计分，不伪造ONNX网络计数。实际messages/prompt/raw/token/身份保留；只编码模型原生模板，不新增语义提示/规则。
- [x] 七个条件各完整十例，最终仓库工具另跑十例；全部实际字节由最终parser和独立复核逐例重算。无合格候选，未扩跑候选原八例或挑种子复验；原套件及标签不改。
- [ ] 可选固定本地profile接入仍待合格候选。本轮没有生产改动，不修改公开API、取消/预算/并发/来源/隐私或离线边界，普通召回不调用模型，默认反思关闭。
- [x] 相关40项、strict mypy145文件及probe/测试6文件、Node检查通过；独立复核21项及实际80次重叠诊断输入/计量/报告核对。67个Core/worker与既有安装相同，无需重建或重复宿主/取消接线验收。诊断增量与报告提交推送，#120/#106按实际结果同步，日用未准入。

Ruling: 用固定语义消息比较权重/量化/执行后端，禁止通过继续改提示选一个过分的结果 — 目前错误已位于模型语义判断且ONNX全序列输出带来资源成本 — 代价是新后端须独立验证离线/计量/取消与真实模型质量，开发诊断通过也不等于日用准入。

Ruling: 首轮Qwen3.5-2B贪心6/10、四负例错误肯定0，未达接入线；仅对该资源较低候选预先追加两个官方设置：文本非思考采样（temp1/top_p1/top_k20/min_p0/presence2）固定seed12091，原生thinking文本采样（temp1/top_p.95/top_k20/min_p0/presence1.5）固定seed12092 — 模型卡说明支持两种模式，需检验推理开销而非追加语义提示 — 每种完整10例、原预算不变，token计量包含全部思考；未完成或错误不得换seed求过。若达到筛选线再按相同条件独立复验，不用best-of。此选择在见追加设置结果前记录。

Ruling: 首批三模型及两个推理设置均未达筛选线，追加且仅追加Qwen3.5-4B的Unsloth UD-IQ2_M量化（固定e87f176479d0855a907a41277aca2f8ee7a09523、1759997088字节、cb4c45ca42c0d7777667e94e3963f8e59e939b3665098328ce601feb5be129c1），沿用贪心/非思考和原消息 — 小模型保守拒答较多、GGUF资源成本下降，需实测更大参数/更低位宽能否在2GiB内改善语义 — 量化来源非官方，另行保留身份；不改变筛选线，不继续换提示或逐题调参。

Ruling: 4B低位宽已出现原话改写/引用缺失，补最后一个同模型精度控制：Qwen3.5-2B Q6_K（同f6d5376be1edb4d416d56da11e5397a961aca8ae，1574961408字节，fc90339420b4298887aafb307a4291c55440b730133bbffe6ba9630503dcb548），不增加模型品种，仍用贪心/原native模板 — 要区分Q4量化损失与2B能力上限，文件规模仍可能满足原资源目标 — 与Q4比较只认完整集，不再追加量化、seed或提示条件；不满足筛选线则只交付评测结果。

Final: Qwen3.5-2B Q4/Q6均6/10、可回答4/6，资源下降但未满足用途筛选；原生thinking为空通道且其采样有1次query failure。其他条件均保留失败，未接入产品。详见docs/reports/local-reflection-candidates-2026-11-13.md及机器摘要；#120和日用继续开放。

## Task 10 — #120 错误弃答定位与定向候选

基线d80ee37。按systematic-debugging先追踪输入→原始生成→解析/严格评分，不用最终fallback猜模型原因。按writing-plans内联执行，用户已明确常规实现不确认。

- [x] 复现两焦点例并发现--file吞掉native前缀换行，RED→GREEN修复为--binary-file并核对实际读取字节；原生Jinja与新路径25个token ID相同。旧native统计更正为受影响，修复后重做五种单变量诊断，均明确非准入，不改角色充当修复。
- [x] 固定LFM2.5 Instruct/Thinking两个作者Q8权重和参数，正确前缀完整十例及2B基线重验，原预算/评分/资源目标不变；Thinking十例用尽512未闭合，失败也保留真实计量，不伪记0。
- [ ] 合格profile接入仍待后续：正确前缀Qwen3.5-2B严格6/10、positive4/6，两个LFM不合格。本轮未扩跑候选原八例或种子选择，不改生产路径或日用门禁。
- [x] 相关45项、mypy146文件及probe/测试8文件、Node检查通过；独立10项及30+14原字节/资源/输入复核通过。67个Core/worker与既有安装一致，不重复安装/宿主验收；诊断修复和更正记录提交推送，#120/#106按实际结果同步，无关改动保留。

Ruling: 两重点例基线均原始空弃答，role改user仍失败；等义描述问法两例均严格答对；删除空弃答字面例只使tool-data答3项（严格值仍失败），删除来源确认句后tool-data答3项、assistant-log答陶艺，四负控均非肯定 — 输入/解析不是丢失事实，暴露措辞与安全条件下“描述来源内容/采纳用户事实”语义混淆；不能据此宣称唯一原因 — 所有变体仅诊断，不算原冻结成绩，不修改生产提示或把source角色升级。

Ruling: 本轮仅评估LiquidAI/LFM2.5-1.2B-Instruct Q8_0（8ed288026e23958ad9dfa92d53ed773a8eee7125，1246253888字节，f6b981dcb86917fa463f78a362320bd5e2dc45445df147287eedb85e5a30d26a）与Thinking Q8_0（b71945805f7d4d87dabd73e26098d44bcad6c35f，1246257792字节，e346f3d63c9e44258019a044b7993d74a0952bc31873c8954f3b86a5caa97e46） — 换训练家族及真实reasoning权重，避免再把2B空通道当思考；Q8减少低位宽引用损失混杂 — 原冻结消息/评分不变，先用官方本地参数temperature0.1/0.05、top_k50、repeat_penalty1.05，其他过滤中性；固定seed120101/120102，完整十例，不逐题选seed，不加新模板。来源为作者GGUF、LFM1.0许可单列，不发布或自动下载权重。

Ruling/更正: 在LFM技术对照发现native --file会删除编译prompt末尾换行（b12360 common/arg.cpp原有行为）。同一两消息输入，旧路径24个prompt token，原生Jinja25个；改--binary-file后25个且ID逐项相同 — 这是确定的输入传输缺陷，先前保存prompt不等于真实消费token，前述反事实诊断和Task9 native结果均须标受影响，不能据此归因模型能力上限 — RED回归固定完整生成前缀；修复后复跑2B基线与两个LFM完整集，再重做诊断，原gold/评分和源码Node模型路径不改。

Final: 正确前缀下tool-data已从空弃答变成3项，严格gold仍3而失败；assistant-log仍空弃答，等义描述问法/删除来源确认句可改变结果，只作因果证据不作为生产改写。修复失败通道丢usage，实际单例816输入/512输出/without_eog留存。无模型达到事前筛选线，生产不接入；见docs/reports/reflection-abstention-2026-11-13.md。

## Task 11 — #120 引用接口、约束解码与较强本地权重

基线52fd11f。先将格式负担和语义能力分开测量；本轮仅admission合成诊断，现有生产接口不先行改动。

- [x] 提供flat-schema格式控制、refs引用接口及refs-schema条件。refs仅替换support的格式说明和原三个演示的对应字段，不新增语义句/示例。模型自己决定decision、原样value及引用ID集合；程序只绑定已选ID的完整当前quote，不能补ID、改答案/标签或截短原文，超过现有2048支持跨度上限则失败。分别记录模型原始wire与机械投影，不把原话绑定归功于模型。
- [x] 现有2B Q4三条件各完整冻结十例：flat-schema、refs（无grammar）、refs-schema；基线使用已核对的正确前缀十例。schema仅约束结构/可用ID，允许空引用由原评分失败，不强制多跳全部来源，保留全部只读动作，不使用gold生成schema。原始问题/角色/安全语义不变。
- [x] 固定Qwen3.5-4B UD-IQ3_XXS（e87f176479d0855a907a41277aca2f8ee7a09523，1949047968bytes，00aab66a2359a9afc52f0ab757e61f9be1656e6fcaba8468bd95579a8b2cbc40），贪心seed12009，比较flat与refs-schema。为容纳更高量化精度，两个条件统一physical batch32、flash-attention on、K/V q4_0；逻辑batch128、context4608、4096输入/512全部输出/30s/4线程不变。新执行条件不和旧2B直接声称单变量因果。
- [x] 沿用严格>=8/10、positive>=5/6、四负例错误肯定0及完整有效输出，owned private和working-set均<=2GiB；250ms采样局限/任何query failure明确保留。不通过不接入，不挑seed/逐题调参数。通过后才做相同条件复验与原八例，再决定生产接入；日用门禁独立。
- [x] 回归、类型检查、最终diff/独立复核，单次相关提交推送并同步#120/#106，保留无关工作区改动。

Final: 五条件严格6/4/4/6/6，可回答4/2/2/3/3，无条件达到原筛选线。4B refs-schema修复助手历史描述、数量和转述理由三焦点例，但三个多跳全失败；两个4B工作集约2105.4MiB，超过2GiB。不接入生产、不扩跑候选原八例，#120/default-off及日用门禁保留。51项、strict mypy147+9/Node及50条原字节重算通过；详见docs/reports/reflection-structured-2026-11-13.md。最终独立复核与提交信息同步GitHub。
