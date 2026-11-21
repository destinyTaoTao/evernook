# 单次语义距离计算的公开召回对照

两组 36 对公开召回均保持有序证据、分数和覆盖状态一致，距离计算次数减少 20%。收益随查询变化。十万真实 MiniLM 的历史查询和百万合成满覆盖的无答案查询，样本 p95 分别下降约 21% 和 31%。六种查询与规模组合仍均超过 1 秒门槛。

## 十万条真实 MiniLM 向量

8 轮共 24 对召回，完整返回页在排除扫描计数后逐对相同。两条命中查询各返回 10 条，近期条目的首位是 `m0099999`，历史条目的首位是 `m0000001`。无答案查询的 `hits` 为空，状态为 `no_matching_evidence`。每次距离计算从 125,000 次降至 100,000 次，减少 20%。历史查询的 p95 下降约 21%，近期 p95 基本持平，无答案 p95 上升约 6%。

| 查询 | 基线中位数（秒） | 候选中位数（秒） | 基线 p95（秒） | 候选 p95（秒） | 每臂样本及 p95 秩 |
| --- | ---: | ---: | ---: | ---: | --- |
| 近期 | 11.494 | 10.453 | 14.931 | 14.990 | 8，取第 8 项 |
| 历史 | 12.777 | 11.305 | 16.852 | 13.344 | 8，取第 8 项 |
| 无答案 | 7.448 | 8.480 | 10.290 | 10.902 | 8，取第 8 项 |

## 百万条合成指纹向量

4 轮共 12 对召回，完整返回页在排除扫描计数后逐对相同。命中查询各返回 10 条，近期条目的首位是 `m0999999`，历史条目的首位是 `m0000001`。无答案查询的 `hits` 为空，状态为 `no_matching_evidence`。每次距离计算从 1,250,000 次降至 1,000,000 次，减少 20%。近期和历史样本 p95 分别上升约 8% 和 2%，无答案样本 p95 下降约 31%。两组共 72 次调用均返回完整覆盖，无超时和降级。

| 查询 | 基线中位数（秒） | 候选中位数（秒） | 基线 p95（秒） | 候选 p95（秒） | 每臂样本及 p95 秩 |
| --- | ---: | ---: | ---: | ---: | --- |
| 近期 | 95.794 | 96.185 | 102.456 | 110.944 | 4，取第 4 项 |
| 历史 | 90.997 | 91.027 | 97.435 | 99.039 | 4，取第 4 项 |
| 无答案 | 44.579 | 37.716 | 55.288 | 38.273 | 4，取第 4 项 |

## 方法

用相同的已建索引账本比较两份 Core。基线为 `8284472`，候选在该版本上合并全局和近期语义通道的距离计算。两臂均通过 `candidate_index_enabled` 核实两个候选 B-tree 已启用，使用相同的 384 维空间和 `sqlite-vec-0.1.9-exact`。每组先向仓库外复制一份 SQLite 快照。两个独立 Python 进程串行从同一副本调用公开 `SemanticRecall.recall`。

十万条夹具使用留存的真实 MiniLM 全量向量。百万条夹具使用留存的 BLAKE2s 合成指纹全量向量，测量精确扫描成本。两种夹具均使用合成原文并已完成词法维护。[十万真实 MiniLM 夹具](2026-11-21-recall-candidate-index-scale.md) · [百万合成满覆盖夹具](2026-11-21-synthetic-full-vector-scale.md)

查询覆盖近期条目、历史条目 0000001 和无答案。固定 `as_of=2026-11-04T00:00:00Z`、`limit=10`、每次调用上限 120 秒。每轮轮换查询顺序，并交替先跑基线或候选。计时从调用公开入口开始，覆盖授权、查询向量生成、词法与语义候选、排序融合及回源核验。事务结束后另存两份完整 DTO。逐对比对完整页时只排除 `semantic_scanned_chunks`，同时校验首位来源、证据状态、两种索引覆盖和降级字段。

p95 使用最近秩 `ceil(0.95 × n)`。三种查询独立统计。小样本的 p95 会落在最大样本上。操作系统没有清除页缓存，两个源读取进程在测量窗口内持续复用。十万条首轮与短时回归验证重叠，测时窗口与本机模型验收分开。内存峰值、硬件温度和系统后台活动未采样。探针计时范围限于直接 Python API，正式规模门槛还需冷启动、服务传输、并发及资源峰值验证。

另用 7 条合成来源比较多段、等距离候选。按 6 个时间和当前输入排除场景、两种候选索引状态与两种距离后端，验证 24 对页面。在排除扫描计数后全部相同。最终 SQL 的 `EXPLAIN QUERY PLAN` 显示一条 `SEARCH c`，共享的 `scan_stats` 仅汇总一次。

## 复现

[公开召回探针](../../probes/admission/recall_shared_scan.py) 在新证据目录写每轮每臂的完整页、原始时长和状态、页摘要和去掉扫描计数后的摘要。页摘要取 SHA-256，JSON 使用排序键、ASCII 转义及无额外空格的编码。启动清单保存实际载入文件的摘要、嵌入空间及索引定义。每完成一轮更新 `checkpoint.json`，全部原始样本可从 `timings.jsonl` 复核。出现错误、覆盖不全或页面差异时保留失败时长和结果，进程返回非零。各 `src` 目录需位于独立的源码树，源库和新证据目录需在源码树外。

原始证据在 `%TEMP%\evernook-shared-scan-055d20ef7d\100k` 和同级 `1m` 目录。另用校验脚本从 72 份完整 DTO 重算摘要、逐对核对覆盖与来源，并从原始样本重算中位数和 p95。基线和候选树的 Python 与 JavaScript 模块仅 `semantic.py` 摘要不同。测后发布的探针补齐类型标注和子进程管道断言。[逐样本计时和摘要](2026-11-21-shared-semantic-scan-scale.json)

| 核对对象 | SHA-256 |
| --- | --- |
| 基线 `semantic.py` | `85a8e57375b84d1f236a7e9c05fad7ab01ab81feef2ea52d2e43e2ec78a58b02` |
| 候选 `semantic.py` | `f593f3ed4d148436251209b538c5957331d6ebc1f248c91cc8b18b962f926ba1` |
| 实测探针 | `693a91cd937022a9e207bc81afc46f1d805e208c204286f19a7742063fb251a6` |
| 发布探针 | `3838e75d40d34bf4081359874daf7c0468a0f99b925898d4a3e4b44d570dc58c` |
| 十万条 `timings.jsonl` | `564847b9b965f35800085ec4f64745a21cc128f21e0f767aefe3e21469414f83` |
| 百万条 `timings.jsonl` | `c7d690259dc3bfb1090e9cbd76e1d99b45d489d656bd573d61921d44b4703028` |

```powershell
$runId = [guid]::NewGuid().ToString('N')
$baselineTree = Join-Path $env:TEMP "shared-scan-baseline-$runId"
git archive --format=zip --output="$baselineTree.zip" 8284472
Expand-Archive -LiteralPath "$baselineTree.zip" -DestinationPath $baselineTree
$baselineSrc = Join-Path $baselineTree 'src'
$candidateSrc = (Resolve-Path src).Path
$fixtures = Join-Path $env:TEMP 'evernook-candidate-index-d95f334e30'
$fullVectors = Join-Path $env:TEMP 'evernook-20261121-full-384-controlled'
.\.venv\Scripts\python.exe -m probes.admission.recall_shared_scan --baseline-src $baselineSrc --candidate-src $candidateSrc --source (Join-Path $fixtures '100k-after') --embedding-config $env:EVERNOOK_SCALE_EMBEDDING_CONFIG --output (Join-Path $env:TEMP "shared-scan-100k-$runId") --size 100000 --rounds 8
.\.venv\Scripts\python.exe -m probes.admission.recall_shared_scan --baseline-src $baselineSrc --candidate-src $candidateSrc --source (Join-Path $fullVectors 'runtime') --output (Join-Path $env:TEMP "shared-scan-1m-$runId") --size 1000000 --rounds 4
```
