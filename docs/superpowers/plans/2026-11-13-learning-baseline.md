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
- [ ] 实测冻结对照、独立评审、逐票提交推送与tracker实况。

Ruling: 选择现有BLOB的C/Python精确搜索，不新增ANN索引 — 位置内核已证明固定前缀漏检，C标量降低小样距离计算成本并沿用现有控制/备份/删除语义 — 代价是全历史线性成本，百万规模p95仍可能需要后续索引选型，不能颁发日常准入。

Final: fixed #112独立复核的直连MCP取消、尾部空白、refresh提交前取消及语义错误阻断scheduler — 可复现问题分别有回归覆盖；新关键词重复行问题亦RED→GREEN，最终相关167 tests passed，strict mypy src/tests 122文件通过。
验证：源码dsh服务4场景及长输入双预算场景通过；实际Core0.1.16/preview.23/固定打补丁rc.5宿主4场景通过。最终wheel53个Core代码/worker文件与源码/安装逐字节hash匹配，npm37文件匹配；安装服务无宿主时真实MiniLM向量化及长输入意图通过。10004来源真实位置全部索引，最终三位置3/3，原prefix仅1/3；小套件失败/缺失及质量/规模门禁保留。见docs/reports/long-term-recall-2026-11-13.md。

## Task 3 — #113

依据冻结偏好、临时指令、否定、引用和冲突案例扩展preferences.py/automatic_preferences.py，复用候选及原子发布。来源可回溯，逐类准入，阶段统计明确，实际宿主下一轮使用新值。高风险保留确认。

## 后续任务

#118综合观察、#119主题视图、#120显式反思分别补充具体实施步骤，复用已冻结分项和控制失效契约。
