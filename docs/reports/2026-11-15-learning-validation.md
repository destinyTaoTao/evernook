# #121 / #148：补齐可自动执行的真实采集阶段

> 资源勘误：本文旧 PID 父链峰值归属未经验证，撤回资源验收用途。修复后完整词法控制另采 33 个场景：retrieval 21 过/12 败，evidence 19 过/14 败，automatic 33 过/0 败；三维均无缺项，总计 73/26/693。该新控制补齐旧 32 例控制的处理器阶段，未覆盖另外三个 arm 或判断/维护/宿主维度。Job 窗口 257 次采样、1 次查询失败，观察 private 峰值 120684544 字节，非硬峰值。[总报告](2026-11-15-autonomous-validation.md)保留归属证据。

固定 zh-learning-v1 的 raw arm 已实际采集全部 33 个场景，之前跳过的恢复、确认删除、处理器切换与三处大位置场景均执行真实协议。冻结套件 SHA256 仍为 `b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`，policy/scorer 未改。

完整 MiniLM + ready lexical 结果：retrieval 24 过/9 败、evidence 22 过/11 败、有限 automatic 33 过/0 败；这三维均无缺项。judgment、maintenance、host_answer 与其他三个 arm 尚未由这个采集器实现，仍按缺失保留。整个 792-cell 评分为 79 过/20 败/693 缺，不构成质量准入。无模型的独立词法控制为 32 个场景，保留 processor_changed 缺项，不能与完整模型采集混成一个成绩。

恢复先对初始旧备份重放最新控制，实际发布因后续派生状态不足被拒为 `archive_evidence_incomplete`；保留拒绝后，用真实完整备份经同一重放/发布 API 恢复，再查询确认来源不复活。没有绕过发布校验。删除在来源入库后创建真实 preview，后续独立合成确认入库后调用 PurgeWorkflow，原文删除结果与查询一并留证。

处理器切换显式调用外部非 editable Core 0.1.12，运行其真实 PreferenceUpdates.configure；随后当前 Core 重新打开该合成运行库，实际旧准入 hash 与当前 rules hash 不同，readmitted=false。初始/重放查询仍是当前 Core；报告没有把它们冒称旧版查询或把一次查询重命名成换版。给相同当前安装的独立检查实际拒绝 unchanged_processor。凭证仅经子进程 stdin 传递，不写命令、公开日志或仓库。

位置场景实际保存 10004 条原文，使用有界 submit_many 与语义维护窗口，不减少来源。有限偏好处理仅对大夹具使用既有 has_candidates 资格筛选并记录跳过数，所有原文仍存在。实际 Core 查询不按 gold 补入证据，也不生成模型判断或宿主答案。

collector/scorer 联合 20 项通过，相关 strict mypy 通过。新增回归覆盖默认缺项语义、真实恢复/删除、外部旧 Core 边界。外部 evidence 为本轮 campaign 的 `learning-full-lexical`、`processor-transition-v2`、`unchanged-processor-check`、`learning-full-minilm` 及 monitor。完整 MiniLM 窗口 204.288 秒、816 采样、Private Bytes 观察峰 700428288、4 次查询失败；其他实验同期运行，采样不能授予资源/时延准入。raw 判断的进一步本地模型诊断另报，不把独立窗口当成完整宿主端到端。

## 2026-11-21 观察候选与主题视图续验

`probes/admission/observations.py --full-lifecycle` 和 `probes/admission/views.py --full-lifecycle --max-fixture-sources 10004` 使用与 raw 和 reflect 相同的合成生命周期。两组都实际确认删除并尝试旧归档恢复，见到 `archive_evidence_incomplete` 后经真正的审查发布走完恢复。未启用旧观察或视图处理器，相关场景按缺失保留。

| 采集内容 | 实际场景 | 检索过 / 败 / 缺 | 证据过 / 败 / 缺 |
| --- | ---: | ---: | ---: |
| 观察审查候选 | 29 | 8 / 21 / 4 | 8 / 21 / 4 |
| 词法主题视图 | 32 | 21 / 11 / 1 | 19 / 13 / 1 |

观察候选发布数为零，固定 64 条来源上限使三个 10,004 条位置场景保留为缺失。主题视图真写入 10,004 条原文并在三个位置场景返回目标。来源控制之后，主题视图返回的独立确认文本照实参与评分。两组均只评分已观测的候选或来源证据，判断、维护、自动更新与宿主回答留空。

初次实际结果摘要 SHA-256 分别为 `485d89eeb011e2409ecdec84d38c3a46490d4eb9fd23158c4384726244491999` 和 `5c05408f7f646ee3b546bed72dcbbdc66938b235c86dadb7565299a19a4391a1`。共享 raw 夹具重跑 33 个合成场景，与原 Job 词法对照的规范化 payload 逐项一致。各采集窗口使用原冻结语料与评分，质量和日用准入仍待通过。

最终采集器对明确不可用的索引、超时和暂忙保留错误并记为缺项，独立后续用例可继续采集。两组用最终代码复跑，分数与初次相同。观察和视图复跑分别耗时 23.29 秒与 61.34 秒，摘要 SHA-256 分别为 `02db9946e4de96bfc35929bc3fed2135ac8a26e8d8f580fd48241beb26a67558` 和 `acbbc279212913b6caed769f24dba4d1f0e97a916cd8a4438feab506ababfc04`。

最终 `test_learning*.py` 29 项通过，相关采集器与测试 7 文件通过 strict mypy。
