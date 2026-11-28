# 百万条合成原文的生产 MiniLM 向量建库

固定生产 MiniLM 公开状态为 `ready`，1,000,000 条原文、1,000,000 条已索引、1,000,000 个向量，待处理和失败均为 0。生产分词器实测 1,000,000 个不同的未填充输入序列，每条 11–25 个 token。没有按八个主题复用代表向量。

| Windows Job 监控窗口 | 已提交来源 | 时间 | 观测进程树 Private Bytes 峰值 |
| --- | ---: | ---: | ---: |
| 四个生产 worker，检测到超限后停止 | 16,384 | 26.407 秒 | 2,339,315,712 B |
| 三个生产 worker，续建并配置候选索引与语义覆盖检查点 | 983,616 | 1,763.841 秒 | 1,848,299,520 B |
| 独立分词清点 | 1,000,000 | 80.115 秒 | 830,189,568 B |
| 读回百万向量、公开模型重算及恢复检查点 | 1,000,000 | 193.260 秒 | 591,261,696 B |

四 worker 超过 2 GiB，停止后原子回执成功续接。两个建库窗口活动时间合计 1,790.247 秒，中间停顿 11.003 秒，从启动到首次全覆盖的墙钟跨度为 **1,801.250 秒**，超过冻结的 1,800 秒预算。这个跨度不含快照复制（17.395 秒）和独立核验，不能认定正式规模门禁通过。监控约每 250 毫秒采样，两个建库窗口分别有 105、7,045 个样本，进程查询失败为 0；峰值是采样观测。

生产向量空间 ID 为 `d37c004d41b73a576aad1d8e6ed2b69dd299281a0991eb8d2269af3989e1695f`。2,594 个连续批次的来源 ID 和推理字节摘要合并为 `eff87282a7a8a650af17fad998091950803b5df9a82bc544ea753a929c03791e`。按来源顺序计算的分词清单摘要为 `cc2483d889a3231d7c47b71d917df90a4c9dc451f5d675587acb5df06626b57d`。

独立读回的 1,000,000 条来源与向量全部匹配批次回执，连续清单 SHA-256 为 `b4001a6b4ac4f6fd8052eb0aa34b1a7f77241dcf56cb001a779a824e21a5208f`。在第 1、16,385、492,033、999,985 行各通过公开模型入口重算一批，64 个 float32 向量逐字节匹配。读回和重算共 31.771 秒。

SQLite 备份保留了词法内容，但旧检查点的 schema 版本已与副本不符。使用公开维护接口重建后，词法及语义状态均为 `ready`，各覆盖 1,000,000 条。已核验启用词法 ID 加速、词法检查点、来源候选索引和语义检查点，本次没有配置 Int8 向量筛选。交给服务采样前，主数据库为 6,459,949,056 字节，整个实验目录文件合计 7,127,385,776 字节。这是交付时磁盘占用，未采集连续磁盘峰值。建库 Core 为 `b6c8396`，独立核验为 `74ed38c`，Python 3.14.4、SQLite 3.50.4、Node 24.12.0。分词和核验有时间重叠，均在正式服务读写采样前结束。[实测回执、模型身份与资源日志摘要](2026-11-28-production-minilm-million.json)

## 测量口径

输入是此前通过公开写入接口保存的百万条合成中文原文。第一个来源是观鸟偏好，其余来源各有唯一七位条目号，轮换八类主题。每条原文只需一个分块。语料用于测建库和查询规模，主题和文体高度重复，不能作为真实用户记忆质量评测。

只读检查了本机两处工程验收归档中的 22 份大库，21 份在临时目录、1 份在 Documents/Codex。其 MiniLM 空间均仅覆盖 10,352 条。已有百万指纹的库属于另一个空间。本轮复制百万原文和既有词法数据，在仓库外重算每条原文的 MiniLM 向量，原有 10,352 条也重新推理。

使用生产配置的 `Xenova/paraphrase-multilingual-MiniLM-L12-v2`、revision `2c4055b12046f11709e9df2c122e59ffbdc2f900`。生产 Node worker 在线路层禁止网络访问，使用锁定的 Transformers.js 4.2.0、ONNX Runtime 1.24.3、q8 图、mean pooling 和 L2 归一化；向量落盘为 384 维小端 float32。每个 worker 使用 CPU 两个算子内线程、一个算子间线程，每批 16 条。读取服务可直接选用同一生产配置与空间。机器为 Windows、Intel Core i5-13600K；建库未使用 GPU。

每次入库先比对完整原文、来源标识、序号和回执 ID，对齐状态版本和内容版本。固定合成库独占维护期间，在事务中同时提交文档、向量和该批来源 ID 加向量字节的 SHA-256，重启从最后一次已提交的来源继续。模型文件在建库边界核对哈希。该夹具采用批量派生表写入；它省去了面向活动账本的每批模型资产、授权和并发撤回检查，不能据此推算公开增量维护吞吐。

原始运行目录为 `%TEMP%/evernook-potion-mixed-20261128-01`。目录名来自备选模型预检。实际建库空间、权重、分词器及采样服务均按结果文件中的 MiniLM 身份核对。

## 复现

准备冻结的百万合成原文快照、锁定的本地 MiniLM 配置、项目验收环境和新的仓库外目录。本次输入为 `%TEMP%/evernook-20261120-validation-zrv0qpq1.p4r/scale-1m-diagnostic`。若从零生成，先按已有复现步骤运行 `scale_ingest` 和 `scale_index`，得到带 `manifest.json`、完整词法索引、写入和维护身份，以及至少已初始化目标 MiniLM 空间的夹具。[从公开入口生成源夹具](2026-11-21-synthetic-full-vector-scale.md#复现与回执)

在 PowerShell 中从仓库根目录执行下列命令。建库时间窗由现有监控器执行；发生中断后在新监控目录用同一个 `--root` 续跑，统计累计时长及每次失败。

```powershell
$python = (Resolve-Path '.venv/Scripts/python.exe').Path
$env:PYTHONPATH = "$PWD/src;$PWD"
& $python -m probes.admission.real_minilm_million clone --source $frozenMillionFixture --root $run
& $python -m probes.admission.monitor --output "$run/build-window" --max-seconds 1800 -- $python -u -m probes.admission.real_minilm_million build --root $run --config $embeddingConfig --workers 3
& $python -m probes.admission.verify_minilm_million --root $run --config $embeddingConfig
& node probes/admission/real_minilm_token_inventory.mjs $run $embeddingConfig
```

核对脚本从 SQLite 读回百万个向量，逐批与推理回执对照，并再次调用公开模型入口重算四批、不相邻的 64 条。分词清单使用生产 Transformers.js 的 `AutoTokenizer`，逐条计算不填充、不截断的 token ID 序列，报告按 SHA-256 去重的个数和长度分布。[建库探针](../../probes/admission/real_minilm_million.py) · [落盘核对](../../probes/admission/verify_minilm_million.py) · [分词清单](../../probes/admission/real_minilm_token_inventory.mjs)
