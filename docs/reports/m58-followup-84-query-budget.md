# #84 — bounded lexical fallback

Scope: small public Core/context/MCP regressions before long-scale admission. The public
search API now accepts a finite positive `timeout` (default 30, maximum 120 seconds) and
an optional in-process cancellation event. Interrupted queries fail explicitly; no incomplete
scan is returned as a complete empty history or an unstable partial offset page.

The same budget reaches SQLite VM progress and Unicode matching inside a large original.
Recall passes its remaining budget to all three lexical channels and preserves its
`recall_timeout` / `recall_canceled` errors. Context reports incomplete coverage. Best-effort
audit writes no longer wait on a writer lock; ordinary writes keep their existing transactions
and busy timeout. This is cooperative cancellation of SQL/Python scanning, not preemption of
arbitrary OS I/O or a hard-real-time guarantee for every other context phase.

The red regression used one 960000-byte synthetic original: a 10 ms budget previously returned
a complete empty result after scanning it. It now fails at the scan boundary. Real SQL-VM
fault workload, in-flight cancellation and the next successful query are also covered.
The Python MCP test transport explicitly selects this checkout's source, avoiding accidental
imports from another editable checkout.

Before/after used the same original, interpreter/hardware and public `SemanticRecall` 10 ms
budget, three observations each. Baseline `13c3545` exited with `recall_timeout` after
172.220 / 163.353 / 170.181 ms; fixed source exited with the same error after
19.244 / 20.511 / 21.399 ms. Owned-tree sampled peaks were 84574208 / 87232512 bytes;
whole probe windows were 1.261 / 0.763 seconds. These are responsiveness diagnostics with
the same unsuccessful recall outcome, not successful recall speedups or formal p95.

Validation: search/context/semantic and real MCP timeout regression **42/42** pass in
40.106 seconds under an explicit 120-second test-run budget. Earlier targeted 18/18 passed;
strict mypy source/tests passes 94 files. Existing exact text, filters, paging, lag/rebuild
coverage, source controls and semantic fallback regressions remain in that run.
Receipts: `evernook-followup-84-small-01/summary.json` and
`evernook-followup-84-validation-02` under the external evidence root.

Full 100k/1M cold/warm/concurrent comparisons remain deferred. Semantic/status and other
non-lexical context phases are not claimed as universally preemptible. No quality/resource
threshold or original #81 observation changed; daily model admission remains blocked.

Spec review caught one remaining CPU boundary: one valid 160001-byte combining run spent
about 0.95 seconds in an indivisible NFKD reorder despite a 10 ms check callback. A public
search regression independently failed its 0.5-second bound (0.974 seconds). Long groups now
decompose individual code points and stably order non-starters by combining class with budget
checks, following [Unicode normalization](https://www.unicode.org/reports/tr15/). Short groups
keep the original implementation. Both the bounded-search regression and independent
normalization comparisons (leading marks, compatibility ligatures, Hangul and case folding)
pass; the pinned normalization identity and source coordinates remain unchanged.

Final run after that repair: **44/44** pass (42.430 seconds), within the same 120-second
operator limit. Spec re-review confirms the combining-run finding is resolved.

Standards review found no blocking rule violation; a small correction to #87's evidence
description is included explicitly to avoid claiming marker writes were real capture.

## Core0.1.6：既有大账本的覆盖统计优化（2026-11-08）

当前Core的真实规模复核定位到覆盖统计成本。原“十万条”合成夹具已在旧矩阵并发写入中增长为**815923条**，仅63493条有有效词法索引，752430条待索引；不能将目录名或旧ingest-report中的100000当作当前条数。对原库做只读SQLite一致性备份，复制到新私有目录后才诊断；原库在备份前后及全部实验结束后的SHA-256一致。访问材料只留仓库外，不改变原库、旧运行环境或日用profile。

此前新建的独立准备批次在定位到旧夹具后主动STOP：267.805秒、13292条、13000份已保存提交时延，292份时延未落盘，明确不算完成十万条或零时延。停止回执root_exited=true、stop_error=null，实际原文与部分回执保留，没有重启覆盖失败或补造数据。

### 反馈与最小改动

旧Core0.1.5在该副本上，近期/历史/无答案/热门词各三次的2秒预算查询均明确`search_timeout`，没有伪完整空结果。进一步缩小为单次近期profiler诊断，约2008.5ms退出；`_lexical_status`约1999.5ms，实际匹配阶段几乎未开始。该观察证明当前时延缺口，不代表超时守卫失效或已找到所有性能根因。

单变量覆盖索引原型将原文总量统计从约694–710ms降至145–148ms，但覆盖统计仍约1.5秒，额外占约61MiB且需约1.30秒构建，因此未采用，已从副本移除。另一单变量SQL对照保持计数/水位完全相同，将覆盖统计从1622–1775ms降至1122–1255ms。

Core0.1.6只复用同一有效索引行集合，直接聚合数量与水位，移除先生成消息ID集合、再第二次回表读取消息的开销。规范化版本、内容版本、FTS行存在及当前可召回状态判定保留；重复FTS行仍去重。不增加索引、表、数据迁移、缓存、服务或依赖，不缩小扫描范围，也不修改超时或质量门槛。

### 实际公开调用与验证

在本批其他拥有的测试停止后，使用同一副本串行运行旧安装版与候选源码的完整热门词查询，10秒调用预算、各三次。旧版3827.9/3937.5/4077.9ms，候选3284.2/3707.3/3385.7ms；整页10条命中、next_offset=10及全部覆盖/来源字段一致。整体24.293秒、150秒操作上限、97次采样/查询失败0，观察峰86679552字节。OS缓存未控制，这不是正式p95、全矩阵、模型或资源准入，不能外推到百万条。

| 检查 | 结果与边界 |
|---|---|
| 行为基准 | 新用例先在旧实现通过，再在新实现通过；覆盖重复FTS行、内容/规范化旧版本、FTS行丢失、抑制最新来源后的计数与水位、补建后回取。不是性能红例。 |
| 源码回归 | 搜索19、语义/维护27、上下文13、真实MCP传输20项分别通过，共79项；两份变更Python文件strict mypy通过。不是全套联合或冻结质量评测重跑。 |
| 安装候选 | 独立非editable Core0.1.6，45文件源码/wheel/安装逐字节一致，依赖冻结为原环境同30包、仅Core版本改变；安装版新增守卫用例通过。 |
| 实际SDK传输 | TypeScript MemoryClient经stdio连接安装包，普通capture/recall身份读取815923条状态与完整热门词页；约3311.6ms、整页与旧版一致，随后关闭客户端。不是原生dsh/provider验收。 |
| 大账本预算 | 安装版单次近期profiler诊断仍约2007ms明确超时；时延准入未通过，不因为局部统计优化便算查询已可用。 |
| 数据/清理 | 原库哈希不变，副本quick_check=ok、foreign_key_check空，无原型debug索引，条数与覆盖水位不变；相关Node/Python进程最终0。 |

wheel SHA-256为`b0601f1cf454623e4ed89774f55c7ea8464078760e25565c4f9bd4ca876daf40`。首次诊断汇总因变量遮蔽失败，原始12个超时样本保留，修正后另跑；首次候选采样与回归测试并行，保留为诊断，不用于上述串行耗时对照。首轮上下文测试模块因目录导入方式未加载，其他27项已执行通过；按项目测试路径单独补跑13项通过，没有修改测试绕过。上述失败、源码原件、profile、采样和对照均保留。

本批证据位于仓库外`Documents/Codex/2026-11-08/evernook-84-100k-01`。没有运行完整100k/1M冷暖/并发矩阵、聊天模型、embedding、冻结224题或正式p95；#81/#84/#87相关剩余项不勾选。Core0.1.6只是独立候选，未切换Core0.1.5/preview.21宿主或日用profile，不继承其1800秒长跑。#105仍按已选路线等待dsh原生完整请求计数接口，ORT原生提案未实施。
