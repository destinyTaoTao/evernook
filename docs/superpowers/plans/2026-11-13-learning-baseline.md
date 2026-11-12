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
- [ ] 按#121提交推送、更新spec和issue。

验证：新旧评测30项通过；mypy显式package bases及MYPYPATH=src;.检查3文件通过。报告docs/reports/learning-baseline-2026-11-13.md。

Ruling: 将冻结前独立复核提前到#121提交前 — 后续各票依赖稳定金标准，等全部实现后再发现评分假阳性会污染比较 — 成本为一次独立评审；无需重复评审已由回归覆盖的修复。

Final: fixed 删除/撤销混淆、历史位置缺口、连续居住证据、宿主自身引文及Recall覆盖率 — 五组负向回归RED→GREEN，最终新旧30/30。
Final: fixed 实现身份从checkout切换实际import包 — 安装路径与字节变更回归通过。

## Task 2 — #112

基于#121的纯同义、全历史位置、长输入和控制失效红例，修改semantic.py/host_context.py及必要服务入口。明确索引候选空间和覆盖，避免固定前缀永久不可见。不为通过而改变冻结语料或门槛。按证据选择向量候选实现，验证旧索引/重建/取消/并发和真实安装入口。

## Task 3 — #113

依据冻结偏好、临时指令、否定、引用和冲突案例扩展preferences.py/automatic_preferences.py，复用候选及原子发布。来源可回溯，逐类准入，阶段统计明确，实际宿主下一轮使用新值。高风险保留确认。

## 后续任务

#118综合观察、#119主题视图、#120显式反思分别补充具体实施步骤，复用已冻结分项和控制失效契约。
