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
