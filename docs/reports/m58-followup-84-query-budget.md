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

### 后续只读SQL/缓存对照（2026-11-08，未采用）

Core0.1.6在同一815923条副本上的公开近期查询再次约2007.5ms明确`search_timeout`。在`mode=ro`、`query_only=ON`连接中尝试合并原文/覆盖统计，查询超过单次10秒守卫后被SQLite progress handler中断；原脚本、已完成基准和失败记录保留，不采用该SQL。

另做默认缓存与单连接64MiB缓存容量的串行只读对照，各三次计数/水位完全相同。默认原文统计732/725/872ms、覆盖1054/1169/1253ms；增大缓存后分别933/885/896ms与584/617/601ms，总统计仍约1.50–1.52秒。OS缓存未控制，没有完整公开查询、并发资源或正式p95收益证明；未更改产品的连接缓存或配置，也不将容量上限当作实际分配量。独立证据位于`Documents/Codex/2026-11-08/evernook-84-status-control-02`，原有完整规模/质量/时延门禁保持。

### 覆盖索引与请求内派生集原型（2026-11-08，路线已确认）

从上批合成副本再以只读SQLite backup建立新私有运行库，源副本主文件SHA-256在备份前后及本批结束时相同。最初815923条、有效词法63493条；没有根据目录名认定十万或百万规模。Core0.1.6源码/安装包保持不变，SQL仅在单独探针进程中临时覆盖，不能称为新产品能力。

| 对照 | 观察与限制 |
|---|---|
| 仅增加覆盖索引 | `messages(id,received,json_extract(receipt,'$.content_version'))`建索引约1896ms，新增已用页65589248字节（约62.6MiB，含复用空闲页，不能等同文件增长）。原文统计769–895ms降至146–168ms，但有效覆盖统计仍1196–1278ms；强制覆盖索引也为1284–1366ms，单独索引不解决主要成本。 |
| ID遍历与回表 | 派生FTS ID集合由DISTINCT改为GROUP BY，再强制使用试验覆盖索引；有效覆盖统计450–524ms，完整热门词页1305–1318ms。原文、内容/规范化版本、FTS存在、去重及可召回性条件保留，整页与原路径一致；不是正式时延准入。 |
| 写入成本小样本 | 之后在同一副本无索引/带索引各提交50条独立合成原文，中位数3.673/3.630ms、最大5.324/15.678ms；全部100条逐字回取、4次幂等重放未增消息。顺序固定、OS缓存未控制，没有并发或p95收益/等价保证。副本因此为816023条，新100条写入不计入前面的815923条对照。 |
| 请求内复用 | 仅在该请求的连接中，先将完整有效ID/received集合写入`temp_store=MEMORY`临时表，再复用它做状态及候选过滤，避免第二次验证同一集合。表不含原文，连接结束销毁；这不是持久覆盖计数缓存或截断历史。产品的缺索引/故障/取消/并发集成尚未实现。 |
| 严格配对公开调用 | 在完全相同的816023条状态上，原Core0.1.6无试验索引3102.0/3530.4/3514.5ms；覆盖索引+分组+请求内复用原型804.3/864.2/853.2ms。六次公开search结果的命中、来源、分页与全部覆盖字段逐字段一致；与100条写入之前的页不混作同一对照。原始页均保留，不是正式冷暖/并发/p95或完整上下文成绩。 |

上述原型仍未达到50ms近期检索门槛，单次热门词低于1秒也不能代替完整ContextBuilder或模型质量准入。新增索引与临时表的峰值内存、并发维护和冷启动成本未独立验收，不能把简单写入样本当作全负载成本证明。原型阶段未改动生产源码、版本、默认缓存、存储格式或日用profile。

首轮验证器直接比较Python tuple与JSON list而失败；独立回取证明JSON规范化后所有字段相同。保留原脚本/诊断及原始页，再进行上述严格配对，没有忽略字段或把首次失败计为通过。最终移除试验索引，确认无持久临时表，三个既有原文与源副本逐字相同；新增100条保留，词法状态816023/63493、pending752530，SQLite quick_check=ok、foreign_key_check空，Core45文件与源码/wheel/实际安装一致。

用户已确认按**可选派生加速**接入：只由显式可信维护入口创建/移除索引，不在旧库启动时自动迁移；缺失时保留原路径，请求失败/取消仍关闭连接并丢弃临时集，不发布不完整覆盖。移除索引不删除原文，也不承诺数据库文件自动缩小。随后实施Core0.1.7，原型脚本、原始页、写入回执和核对记录位于仓库外`Documents/Codex/2026-11-08/evernook-84-covering-03`；#84原AC1/4及#81准入保持未完成。

### Core0.1.7显式接入与实际安装（2026-11-08）

实现`configure_lexical_acceleration(access, enabled=bool)`和只读状态入口，启停须maintain权限，提交完成才返回bool。同名异构对象报冲突，不覆盖或删除。CLI增加`prepare-search-acceleration/remove-search-acceleration`与status布尔状态；未开放为MCP模型工具，不在初始化、普通查询或词法refresh时自动创建持久索引。没有新增依赖或存储版本迁移，旧库与默认路径保持。

识别确切索引定义且FTS/元数据表存在时，查询在原BEGIN读事务内，以分组去重的完整有效ID集合建立内存临时表。它保留内容/归一化版本、FTS存在及可召回性检查；状态和候选过滤复用同一集合，仍逐字回源并保留原排序/分页。savepoint仅隔离可选临时操作，可恢复SQL失败回退同一快照的原路径；取消/超时或SQLite整体回滚仍明确失败，不自动重试查询，也不发布半页。连接关闭销毁临时集。

| 验证 | 实际结果与范围 |
|---|---|
| 红绿与源码 | 新入口缺失的红例保留。47项搜索/加速/目标CLI回归、2项追加类型/并发移除守卫、68项完整CLI/语义维护/上下文/真实MCP分别通过（CLI有重复，不是一次全量联合）。四份Python变更strict mypy通过。 |
| 关键边界 | 原19项搜索契约在显式启用加速后重跑；覆盖旧版本/缺FTS行/重复行、精确坐标、抑制/更正与重建、预算和取消。另验证无自动迁移、启停幂等、捕获身份不能维护、同名冲突保护、temp_store=MEMORY/连接隔离、临时SQL失败回退、取消不伪完整、提交前SQL异常回滚DDL、并发采集和并发移除索引的一致快照。 |
| 安装版 | 独立非editable Core0.1.7，原冻结依赖仅替换Core；首版、LF规范化包及收尾大小写守卫包分别执行相同的30项加速/目标CLI回归并通过，不累计成不同用例。最终45文件与暂存Git源码/wheel/实际安装逐字节相同。 |
| 同状态配对 | 同一816023条副本，最终包未启用2914.6/2978.4/2966.0ms，实际CLI启用后717.5/717.7/722.4ms，CLI移除后3037.8ms；所有命中、来源、分页及覆盖字段完全相同。创建CLI总2419.5ms，含启动和认证，不是纯建索引耗时。首版2988.9/3061.6/3039.4→720.0/732.5/724.6ms原始对照保留。 |
| 实际SDK | TypeScript MemoryClient→stdio→最终包约731.8ms，整页匹配同一基准；普通身份仍capture/recall，尝试调用维护方法返回unknown_tool，索引状态未变。首版792.8ms及首轮LF包724.0ms的独立记录保留，不是原生dsh或真实模型。 |
| 资源观察 | 最终包配对/CLI/SDK整体25.794秒，180秒操作上限；103采样、查询失败1，进程树Private Bytes观察峰205553664字节（约196MiB）。首版25.790秒/103样本/失败0/182796288字节亦保留，不比较资源改善。仅该小型链路，非模型资源/正式p95或硬峰值。 |
| 大副本守卫 | 在真实临时表语句阶段，20ms预算约33.9ms返回search_timeout；SQL开始后定时取消约35.5ms返回search_canceled。两者后新请求均成功且整页一致，未留下部分覆盖。不是50ms成功检索成绩或硬实时保证。 |
| 其他查询 | 同一安装版显式启用后，两轮近期稀有词/历史稀有词/无答案均约2016–2020ms明确search_timeout，热门词726–742ms返回10命中；保留六次失败，没有返回伪完整空结果。 |
| 数据与清理 | 原源副本哈希不变，100条试验新增原文再次逐字核对；副本816023条、有效索引63493、pending752530不伪称追平。SQLite quick_check=ok、foreign_key_check空；实际移除可选索引，无持久临时表，相关Node/Python进程0，运行数据保留。 |

失败记录保留：首次完整CLI在无PyNaCl的冻结环境中有一项保险库测试因导入失败，未改动的安装Core0.1.6同样失败；换到已有PyNaCl的开发环境后完整CLI通过，没有修改/跳过测试。首次大副本验证器对20ms请求的阶段/耗时联合断言失败；随后取消探针误把`cancel.set()`作为已执行结果传入Timer，测到入口取消。保留原脚本、失败日志和诊断，再将回调改为真正的延迟调用、在SQL开始时触发；产品代码、预算与门槛未因这些探针错误改动。

打包末次核对发现部分原文件混合换行，而`.gitattributes`要求LF；首版包和原始性能记录保留。仅规范化已改的runtime.py/README.md工作文件，并从本任务暂存Git blob复制构建源，未修改其他原工作文件。首轮规范化包与首版的8个代码成员仅CRLF→LF，Python AST相同。收尾审查另复现SQLite名称大小写冲突被误报为storage_unavailable：先补红例，再为两处名称查询加NOCASE，保留异构对象并明确报冲突；不是绕过冲突或删除原对象。最终再构建安装，重跑30项、配对性能/SDK/启停和45文件核对；末次DB仍816023条、quick_check=ok、FK空、可选索引移除。原始失败、首版及两个中间构建均保留。

首版性能包SHA-256：`e1c7de2a43a517de2a4cbd0c44fb1fdf7c383d1c8d38f1583e317208ad3040a2`；**最终LF/大小写守卫wheel** SHA-256：`b60e63ac85edaa015add860b7b70b3cc45ec88454fc1eb60ee6bdc8d612e9a56`，位于外部证据根`wheel-final-v2`。首轮LF包`46be0c10ee107c2858853d49e73fdbcabe170ad6ad9576c5ae1c4a644a975e50`留在`wheel-final`。实际安装、配对页、CLI/SDK、采样、失败/修正记录和最终核对位于仓库外`Documents/Codex/2026-11-08/evernook-84-acceleration-01`。未改Core0.1.6/preview.21的独立宿主环境，Core0.1.7没有原生dsh或新1800秒窗口，不继承旧版本证据；#105等待原生计数接口、ORT原生提案未实施，#81/#84完整质量/规模/时延仍未通过，日用未启用。

### Core0.1.8：FTS维护调用内查找（2026-11-08）

针对仍有大量待索引原文的情况，先测真实维护入口而非猜测提交成本。冻结Core0.1.7安装版从原815923条合成副本作只读SQLite backup，显式启用已获授权的可选覆盖索引，再执行一次`refresh_lexical(limit=100)`。初态63493条有效索引、pending752430；带剖析的公开调用13594.1ms，其中100次按未索引message_id删除FTS耗时8664.5ms，待处理选取3125.9ms、覆盖统计1228.5ms；102次COMMIT仅113.3ms。瓶颈主要是重复扫描，未据此合并逐条写事务。

最小实现复用调用内一个连接，一次查找本批来源的FTS行号，逐条删除同时限定行号及message_id；最多缓存100000个匹配行，超限或可选查找失败回到原删除路径。派生计算仍在写事务外，每条重新核验原文/收据/可召回性，分别提交FTS及版本元数据。后续坏收据不能抢先阻止此前合法记录提交；提交前失败回滚当前条，保留已完成前缀。没有新依赖、持久索引、表或存储版本迁移，此优化也不要求可选覆盖索引存在。

并发保护依据[SQLite官方data_version契约](https://www.sqlite.org/pragma.html#pragma_data_version)：只比较同一连接前后值，其他连接提交会改变它，自身提交不会。查找前采样，每条取得写事务后复核；发现外部提交就弃用缓存，继续原路径。这样覆盖其他维护者同版本重建FTS、重用旧行号等场景；不是跨连接比较版本或永久缓存行号。

| 验证 | 实际结果与范围 |
|---|---|
| 红绿 | 2000条合成重复派生行、20条追加来源的VM工作量回归，旧实现4477次进度回调，超过800上界而失败；新实现通过。检验重复全表扫描，未用易抖动的墙钟阈值。 |
| 守卫 | 7项维护回归覆盖外部同版本补行、外部重建/行号复用、缓存超限、查找SQL失败回退、第二条提交失败后的前缀/续跑、后续坏收据的处理顺序及工作量。原文不因维护失败丢失。 |
| 源码检查 | 早期51项维护/搜索/加速联合通过，随后7项完整维护守卫通过；CLI/语义/维护/上下文/MCP另68项通过（分组存在重复，不是全仓全套）。runtime与新测试strict mypy通过。首次测试注解产生Any返回错误，已修正注解并保留失败日志。 |
| 实际安装 | 新非editable Core0.1.8环境，冻结依赖仅替换Core。55项维护/搜索/加速联合通过；45个代码成员与Git暂存blob、wheel、安装文件逐字节一致。真实CLI对独立两条合成原文完成refresh-index、ready和搜索两命中，且没有自动创建可选索引。 |
| 同初态维护 | 两个新副本同批100条：源码4497.6ms，实际安装4718.7ms；与旧版13594.1ms的派生结果一致。源码/安装均只查找一次行号，约99.9/103.1ms；安装待处理选取3178.4ms、两次覆盖统计1176.3ms，剩余主要开销仍在。三次均带同种剖析，顺序/OS缓存未控制，不称冷暖/p95或全负载加速比。 |
| 数据完整性 | 基线与源码、基线与实装分别全表流式比较逻辑行哈希：messages815923条、lexical63593条、lexical_docs63593条一致（FTS比较包含重复行，不比较内部rowid）；全部前后覆盖DTO一致。SQLite quick_check=ok、FK空。排除各环境独立审计观察，不声称整个DB文件字节相同。 |
| 有界资源观察 | 基线/源码/安装分别15.780/6.519/6.770秒，均120秒上限内自然0退出；63/26/27采样、查询失败均0，Private Bytes观察峰92692480/92954624/93048832字节。仅该维护进程树，非模型资源、硬峰值或资源改善结论。 |

CLI探针首次误以为initialize会写访问文件，随后误用已绑定entry的新client_id，均在维护前拒绝。保留这两次原脚本/夹具/日志，按现有CLI测试的显式pair_client流程在第三个全新夹具验证成功；没有重放失败夹具的业务写入、修改权限规则或将探针失败算作通过。

新wheel SHA-256：`a9e9b8ef7224f6ea58f74d1c389a3006c9e6c301c8001af55a762102c6914620`。构建源来自本任务暂存Git blob，未批量修改其他工作文件。原副本SHA-256保持`e5d0d51aaaf357e3353ea4f392c482eb7ed34e51b8d07d95c6ef1f2bc6f5c90f`。原始剖析、三组副本、完整表哈希、监控、红绿/失败日志及安装包保留在仓库外`Documents/Codex/2026-11-08/evernook-84-maintenance-01`；可选索引只在新合成副本显式建立并保留，没有改用户运行库或全局宿主。

本批仅补100条，仍有752330条待处理；未跑75万条补齐、完整100k/1M矩阵、并发负载、50ms/1秒或正式p95准入。Core0.1.8未替换Core0.1.6/preview.21宿主，没有新原生dsh或1800秒证据。#81/#84继续open，#105等待原生计数接口；ORT原生提案仍未编译/加载，日用未启用。下一小批应先处理待处理记录选取和覆盖统计成本，再做有界补跑与多类查询验证。

### Core0.1.9：待处理ID先选取再回源（2026-11-08）

本批从已验证Core0.1.8继续，未更改可选索引的显式启停方式。三项初始假设为：原查询过早读取/排序未入选正文、有效索引集合重复构造、维护未复用既有覆盖索引。先对上批已处理后的同一63593索引合成副本做只读SQL对照：原选取3112.5ms，只强制外层索引1517.4ms，只复用覆盖成员查询2326.0ms；先选ID再回取、成员验证也用既有覆盖索引为802.9ms。五种查询的完整100条event/receipt逐项一致；执行计划确认选取与成员验证均使用covering index，正文仅对选出的ID回表。不把不同查询组合当作单变量收益，也未据此增加新的持久索引或维护临时表。

先补稳定红例：物理上先插入较大待处理原文，再以合法较早接收时钟插入小原文；limit=1应只维护小原文。将SQLite单记录长度上限设为8192后，旧查询在排序筛除之前触及未入选大正文，返回unavailable/1已索引/2待处理，回归失败。新路径先选ID再回取，正常得到behind/2已索引/1待处理，未处理大原文逐字不变。该测试约束实际读取行为，不依赖墙钟或查询字符串的形式断言。

实现仅在确切可选索引已存在时使用新选取，保留原received/id排序和limit；索引校验/选择在同一BEGIN读快照内，选取后COMMIT，再在写事务外派生并逐条核验/提交。缺失或异构索引保持原路径；可选选取SQL失败只在事务仍完整时回退，丢失事务不自动重试。独立lexical_status也复用同一已有覆盖索引，保留完整版本、FTS存在、去重与可召回性条件，可选计数SQL失败回到原统计。原始证据、存储版本、授权范围、模型工具和依赖均未增加。

| 验证 | 实际结果与范围 |
|---|---|
| 源码回归 | 首轮56项联合通过，再补齐后完整61项维护/搜索/加速联合通过；CLI/语义/维护/上下文/MCP另68项通过，runtime和维护测试strict mypy两文件通过。前后分组有重复，不合计为全仓用例总数。 |
| 新守卫 | 本批新增6项，含上述大正文读取红绿、可选选取故障回退、独立状态计数回退、读快照校验后并发移除索引、选取事务丢失时无重试/无索引副作用，以及内容/规范化版本、缺FTS/元数据和重复FTS的有限批次覆盖。原有抑制/更正、外部重建/行号复用、坏收据和提交失败前缀回归继续通过。 |
| 实际安装 | 独立非editable Core0.1.9，冻结依赖只替换Core；61项维护/搜索/加速联合通过，45代码成员与暂存Git blob、wheel、实装逐字节一致。新合成夹具实际CLI维护两条到ready、搜索两命中，未自动创建可选索引。 |
| 同初态公开维护 | 三份新副本均从815923条/有效索引63493的同一原始合成快照开始，同批100条。Core0.1.8冻结安装新基线4544.3ms，Core0.1.9源码1502.4ms、新安装1681.6ms；均同种剖析且完整100条提交。选取3187.5→755.1/759.3ms，覆盖统计1164.7→538.8/562.5ms。顺序和OS缓存未控制，没有冷暖/p95或全负载加速承诺。 |
| 完整数据比较 | 基线与源码、基线与实装分别逐表流式哈希：原文815923条、FTS63593条、版本元数据63593条完全一致（含重复行，不要求FTS内部rowid相同）；前后覆盖DTO一致。quick_check=ok、foreign_key_check空；独立审计观察不在逻辑表比较范围。 |
| 实际SDK→stdio | 普通capture/recall身份访问新安装版，完整热门词页约815.0ms，与本批Core0.1.8基准整页deepEqual；模型调用configure_lexical_acceleration仍unknown_tool，维护权限未开放。这不是原生dsh、真实模型或新检索时延准入。 |
| 有界观察 | 维护基线/源码/安装分别6.520/3.015/3.265秒，均120秒上限内自然0退出；26/12/13采样、查询失败0，Private Bytes观察峰92540928/91983872/92794880字节。SDK窗口6.539秒/60秒上限、26采样/0失败/峰179494912字节。仅各小型进程树，不称模型资源、硬峰值或资源改善。 |

新wheel SHA-256：`20c6f92062b6451cb482d0fb41ebffd1306b0541489dcd49ca4bde2bd4a6f589`；从本任务暂存Git blob构建，没有批量修改无关工作文件。原始快照哈希仍为`e5d0d51aaaf357e3353ea4f392c482eb7ed34e51b8d07d95c6ef1f2bc6f5c90f`。只读SQL计划/原结果哈希、红例、三份新副本、剖析、回归、安装/SDK及采样记录保留于仓库外`Documents/Codex/2026-11-08/evernook-84-maintenance-02`；可选索引仅在新合成副本显式建立并保留，没有修改旧独立宿主或用户运行库。

各组只补100条，仍pending752330，未完成75万条补齐、100k/1M完整矩阵、正式质量/并发/p95/50ms或1秒门槛。Core0.1.9没有新原生dsh或1800秒窗口，不继承Core0.1.6/preview.21证据，日用仍关闭。#81/#84保持open，#105等待原生计数接口、ORT原生提案仍未应用/编译/加载；后续继续有界补跑和当前候选的宿主复验。
