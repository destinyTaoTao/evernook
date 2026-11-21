# 百万原文、百万合成向量的公开召回对照

在同一个 1,000,000 条原文的合成库上，为全部原文构造了独立的 384 维文本指纹。启用候选索引前后，三组公开召回页逐字段一致。单次耗时从 66.021–118.620 秒缩短到 31.848–70.095 秒。**这组结果测量精确扫描和查询计划在满覆盖时的开销。语义质量和真实 MiniLM 的百万条推理、建库速度需另行验证。** 同一语料的[真实 MiniLM 部分覆盖对照](2026-11-21-recall-candidate-index-scale.md)仅有 10,352 条原文建立向量。

| 固定查询 | 无候选索引（秒） | 启用候选索引（秒） | 两个完整 DTO |
|---|---:|---:|---|
| 近期 `合成条目0999999` | 118.620 | 70.095 | 逐字段相同，10 条，首位为 0999999 |
| 历史 `合成条目0000001` | 105.361 | 61.375 | 逐字段相同，10 条，首位为 0000001 |
| 无答案 `外星深海音乐会许可证` | 66.021 | 31.848 | 逐字段相同，0 条且状态为 `no_matching_evidence` |

六次调用均使用公开的 `SemanticRecall.recall`，固定 `limit=10`、`as_of=2026-11-04T00:00:00Z`、120 秒截止。每页 `degraded=[]`，`semantic.state=ready`，检索后端为 `sqlite-vec-0.1.9-exact`。每次累计扫描 1,250,000 个向量块，含近期通道再次扫描的 250,000 个。`semantic_scan_limited` 和 `recent_window_limited` 均为 false。逐字段比较保留证据、版本、排序、分数、通道、覆盖和分页状态。候选索引缩短了这些样本的时间，达到 1 秒目标仍有显著差距。

### 数据与验真

从合成输入的稳定快照，以 SQLite 只读 backup 复制到库外新目录，再批量写入专属 `synthetic-text-fingerprint-384` 空间的派生表。原库中的 MiniLM 空间与本空间独立。指纹使用 UTF-8 文本的 16 字节 BLAKE2s 摘要，在 16 个互不相交的 24 维区间各选一个分量，取值为 +0.25 或 −0.25，其余为零。向量范数恒为 1，同一函数用于语料和查询。

构建时逐条核对冻结生成器的合成文本、用户角色及完成状态、来源元组、消息 ID、唯一序号及所有 1,000,000 个序号的覆盖。构建后在 SQLite 中逐条回源比对状态版本、内容版本、分段坐标、正文长度及每个向量的全部 1,536 字节。1,000,000 份文档对应 1,000,000 个块，失败记录为 0，WAL checkpoint 结果为 `[0,0,0]`。重新打开运行库后，公开 status 返回 1,000,000 个 eligible、1,000,000 个已覆盖来源、1,000,000 个向量、0 个待处理和 0 个失败块。

| 准备过程 | 结果 |
|---|---:|
| 复制快照 | 8.633 秒 |
| 核对库内语料并批量构造合成向量 | 71.799 秒 |
| 全量字节核对、checkpoint 与公开状态验证 | 44.210 秒 |
| 初次通过公开维护接口建两棵候选 B-tree | 6.799 秒 |
| 建索引前／后 SQLite 主文件大小 | 6,065,106,944／6,201,163,776 字节 |
| 对已启用的两个索引执行公开 `rebuild=True` | 9.790 秒 |
| 重建后验证索引定义及两组百万行有序投影 | 22.232 秒 |

六次召回测时结束后，单独执行公开重建。索引名称、定义均经 Core 重新核对。对来源索引按 ID 遍历，比较 ID、内容版本、发生时间和接收时间；对时间索引按发生时间降序、ID 升序遍历，比较发生时间和 ID。每个索引投影都与同一事务内 `NOT INDEXED` 的原表遍历取得相同的百万行计数和摘要。`EXPLAIN QUERY PLAN` 确认两个索引侧各走指定 B-tree，原表侧全表扫描并单独排序。重建阶段验证的是有序候选投影；上表三组完整召回页取自重建前的测时窗口。

百万条向量采用受控夹具批量写入，构建耗时包括原文核对及派生表写入。该方法绕过逐条模型推理与公开增量刷新，不能拿来估计真实建模吞吐。固定顺序为先基线三次、再建索引、后候选三次，每种查询每臂各一次，未清系统文件缓存。结果不提供生产 p95、冷热因果分解、资源硬峰值或多年运行准入。

### 复现与回执

使用 Windows 本机、Python 3.14.4、SQLite 3.50.4。源库位于 `%TEMP%\evernook-20261120-validation-zrv0qpq1.p4r\scale-1m-diagnostic\runtime`，本次完整 DTO 与实测回执在 `%TEMP%\evernook-20261121-full-384-controlled`。公开脚本只在新目录创建副本。[受控压力测试入口](../../probes/admission/synthetic_full_vector_scale.py)

```powershell
$sourceFixture = Join-Path $env:TEMP 'evernook-20261120-validation-zrv0qpq1.p4r/scale-1m-diagnostic/runtime'
$newFixture = Join-Path $env:TEMP ('evernook-full-384-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale prepare --source $sourceFixture --output $newFixture
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale measure --output $newFixture
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale rebuild --output $newFixture
```

若本机没有留存快照，可先用公开写入口从零生成同一套百万合成原文。历史快照已有全量词法维护及部分 MiniLM 派生行；以下前置步骤需要一个可工作的本地 MiniLM 配置，路径由 `EVERNOOK_SCALE_EMBEDDING_CONFIG` 提供。单独的 MiniLM 空间仅用于复现此起点。源消息 UUID 由新库生成，新库应在自己的开关对照中比较完整页面。

```powershell
$baseFixture = Join-Path $env:TEMP ('evernook-ingest-1m-' + [guid]::NewGuid().ToString('N'))
$baseIndex = "$baseFixture-index"
.\.venv\Scripts\python.exe -m probes.admission.scale_ingest --output $baseFixture --messages 1000000 --batch-size 100
if ($LASTEXITCODE -ne 0) { throw '百万条原文写入未完成' }
.\.venv\Scripts\python.exe -m probes.admission.scale_index --fixture $baseFixture --output $baseIndex --embedding-config $env:EVERNOOK_SCALE_EMBEDDING_CONFIG --reuse-embedding --semantic-window 1000 --lexical-strategy checkpoint --lexical-transaction-size 100
$baseStatus = Get-Content (Join-Path $baseIndex 'report.json') -Raw | ConvertFrom-Json
if ($baseStatus.error_code -or $baseStatus.actual_messages -ne 1000000 -or $baseStatus.lexical.state -ne 'ready' -or $baseStatus.lexical.indexed_messages -ne 1000000) { throw '初始语料或词法维护未完成' }
$sourceFixture = Join-Path $baseFixture 'runtime'
$newFixture = Join-Path $env:TEMP ('evernook-full-384-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale prepare --source $sourceFixture --output $newFixture
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale measure --output $newFixture
.\.venv\Scripts\python.exe -m probes.admission.synthetic_full_vector_scale rebuild --output $newFixture
```

`scale_index` 的总准入在真实向量未覆盖全库时会返回 2。上面的检查单独核对源数据数量、词法覆盖和实际错误；随后在副本上验证独立合成空间的百万覆盖。

| 核对对象 | SHA-256 |
|---|---|
| 精确扫描空间 ID | `339d9250bf610015b5895b2ccefc8721143e844c860763cc8a01f0cae1b76b1a` |
| 按消息 ID 排序的来源、原文及版本清单 | `0b791b3e6d4fc31f867c3013968b6d3caac94629ea779ce5b8503e2dbf3669ac` |
| 两臂一致的近期完整页 | `9d5e6ce99b0f465d429b2968b3d6df5d8bc0197f8e54331dfce3a4f2403a8e9a` |
| 两臂一致的历史完整页 | `57daff93a3075988f975e4bccc525fade71976965710931d276957376ae1d380` |
| 两臂一致的无答案完整页 | `e16e8e5aa93c5f3a9eae629627e535f228d6eecf2028de4e11e43380a03e04b9` |
| 重建后来源索引与原表的完整投影 | `9bc8c64d4f16c5145050e0ae5378bee9892032f8037d0aa50e87184bec17669b` |
| 重建后时间索引与原表的完整投影 | `c80539c5252c34fda56e39974e0a6cc640d173baafbddec27b730d3bf93a2d7c` |

页摘要基于完整 page 对象，使用 UTF-8 JSON、排序键、无额外空格，时长单独存放。原 `measurement.json` 保存测时入口、政策和 checkout 中五个 Core 模块的摘要。使用同一工作目录和虚拟环境逐个解析实际导入路径，均为 checkout 下的 `src/evernook`，五个文件摘要与测时回执一致。后续入口直接从实际导入的模块定位文件，并增加了单独的 rebuild 命令。
