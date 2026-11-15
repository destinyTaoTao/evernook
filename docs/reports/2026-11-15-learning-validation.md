# #121 / #148：补齐可自动执行的真实采集阶段

固定 zh-learning-v1 的 raw arm 已实际采集全部 33 个场景，之前跳过的恢复、确认删除、处理器切换与三处大位置场景均执行真实协议。冻结套件 SHA256 仍为 `b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`，policy/scorer 未改。

完整 MiniLM + ready lexical 结果：retrieval 24 过/9 败、evidence 22 过/11 败、有限 automatic 33 过/0 败；这三维均无缺项。judgment、maintenance、host_answer 与其他三个 arm 尚未由这个采集器实现，仍按缺失保留。整个 792-cell 评分为 79 过/20 败/693 缺，不构成质量准入。无模型的独立词法控制为 32 个场景，保留 processor_changed 缺项，不能与完整模型采集混成一个成绩。

恢复先对初始旧备份重放最新控制，实际发布因后续派生状态不足被拒为 `archive_evidence_incomplete`；保留拒绝后，用真实完整备份经同一重放/发布 API 恢复，再查询确认来源不复活。没有绕过发布校验。删除在来源入库后创建真实 preview，后续独立合成确认入库后调用 PurgeWorkflow，原文删除结果与查询一并留证。

处理器切换显式调用外部非 editable Core 0.1.12，运行其真实 PreferenceUpdates.configure；随后当前 Core 重新打开该合成运行库，实际旧准入 hash 与当前 rules hash 不同，readmitted=false。初始/重放查询仍是当前 Core；报告没有把它们冒称旧版查询或把一次查询重命名成换版。给相同当前安装的独立检查实际拒绝 unchanged_processor。凭证仅经子进程 stdin 传递，不写命令、公开日志或仓库。

位置场景实际保存 10004 条原文，使用有界 submit_many 与语义维护窗口，不减少来源。有限偏好处理仅对大夹具使用既有 has_candidates 资格筛选并记录跳过数，所有原文仍存在。实际 Core 查询不按 gold 补入证据，也不生成模型判断或宿主答案。

collector/scorer 联合 20 项通过，相关 strict mypy 通过。新增回归覆盖默认缺项语义、真实恢复/删除、外部旧 Core 边界。外部 evidence 为本轮 campaign 的 `learning-full-lexical`、`processor-transition-v2`、`unchanged-processor-check`、`learning-full-minilm` 及 monitor。完整 MiniLM 窗口 204.288 秒、816 采样、Private Bytes 观察峰 700428288、4 次查询失败；其他实验同期运行，采样不能授予资源/时延准入。raw 判断的进一步本地模型诊断另报，不把独立窗口当成完整宿主端到端。
