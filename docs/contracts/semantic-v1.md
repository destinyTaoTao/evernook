# T15：显式离线语义召回与自适应近期范围 v1

对应 [Issue #16](https://github.com/destinyTaoTao/evernook/issues/16)。复用同一 Python/SQLite 原文账本和[词法契约](lexical-v1.md)，新增可重建分块向量与查询融合。它返回有来源的**候选证据**，不是当前事实、自动沉淀或最终预算上下文；原有 `search` 行为不变。

## 近期是一个维度，七天不是历史边界

用户于 2026-10-30 明确要求：七天只是召回维度，内容稀疏时应能追溯更久，并支持按内容量或时间调整。本契约据此采用：

1. `recent_days` 是近期窗口起点，默认连续 7×24 小时，可显式设为 30、90 等天数（1–36500）。它按查询参考时刻 `as_of` 的实际时间计算，不按本地日历切天。
2. 从参考时刻向前检查当前可召回、已保存普通原文的记录，累计 Unicode code point 字符量。若近期内容不足 `recent_target_chars`（默认 8192，范围 1–1048576），窗口向前扩展至达到目标的记录时间；资料不足时覆盖已扫描的更早记录。
3. 扩展最多检查 256 条记录，防止大量空短内容造成无界维护；遇上限时返回 `recent_window_limited=true`。字符量只是选择时间窗口的密度目标，允许整条记录超过目标，不是精确 token 预算，也不截断原文。
4. **全历史精确/词法与语义路径始终保留。** 时间未知与参考时刻之后的记录只参加全历史路径，不用接收时间伪装成近期；未来记录保留原始角色/时间，不自动成为当前事实。
5. 最终结果为近期候选保留最多 `min(2, limit // 3)` 个位置；没有近期匹配时全部交还全历史候选。达到内容目标不会让无关内容自动获得匹配资格，也不会删掉旧证据。

结果给出实际 `recent_since/recent_expanded/recent_content_chars/recent_window_limited`，调用端能看出近期范围是否扩展及其覆盖限制。`as_of` 默认请求时 UTC，也可给带时区偏移的显式时间，便于复现；没有冻结 T16 的用户时区、必读集合或最终上下文预算。

## 模型与维护

2026-11-15 的显式 Python `refresh_window(access, limit=1000, timeout=120, cancel=None)` 最多快照 1000 个缺失 chunk，推理仍每批至多 16 条。每批在推理前复核权限、可召回性、来源版本及片段；提交前再次复核，独立持久化。取消或失败保留已经完成的批次，重试按真实覆盖续跑；不是整个窗口原子。原 `refresh` 默认及上限仍为 16，没有新增持久化表或 MCP 权限。

`LocalMiniLM.session(timeout<=120)` 允许显式有界复用；可信服务的 `ServiceEmbedding` 持有、轮换并关闭会话。每次仍校验全部固定资产摘要与输入/输出，不缓存结果或改变空间。请求 ID、输出大小、取消与 Windows 实际父进程生命周期均受约束；非法超大整数等输出归为 `invalid_embedding` 并关闭当前会话。普通 `LocalMiniLM.embed` 保持一次性行为。复用或数值不变性测试不能授予模型、性能或日用准入。

`SemanticRecall(memory, backend)` 使用独立向量空间。首个实际适配器 `LocalMiniLM` 仅接本机已有 R0 候选：

- `Xenova/paraphrase-multilingual-MiniLM-L12-v2`，revision `2c4055b12046f11709e9df2c122e59ffbdc2f900`，q8，384 维，mean pooling + L2 归一化。
- Node 24.11–24.x、Transformers.js 4.2.0、onnxruntime-node 1.24.3；安装/模型准备独立于推理。核心库与无模型回退仍只依赖 Python 标准库。
- 每次实际 embedding 校验固定权重/tokenizer 文件 SHA-256，不下载模型，不混用其他权重。适配器与模型文件校验标识、维度、归一化/分块版本都参与空间 ID。
- 原文按 192 code point 分块、重叠 32；片段保留原文 `[start,end)` 与状态/内容版本。worker 在推理前检查未截断 token 数，超过模型 512 token 上限的片段按条目显式失败（输出 null 向量），绝不静默裁掉片段尾部，也不会让单个片段拖垮整批推理。
- 普通核心、CLI/MCP 不默认启用模型；仅可信本地构造 backend 或提供启动配置。转换权重许可链仍未作为发布物验收，代码不分发权重，候选不等于最终生产模型。

可信 Python backend 可以额外同时实现 `embed_documents` 与 `embed_query`，分别接收待索引片段和单条查询，并接收与原 `embed` 相同的 timeout/cancel 参数。角色方法必须成对提供，`EmbeddingSpace.encoding` 必须以 `asymmetric-` 开头并标识两侧变换及版本；只提供一个方法或沿用原对称编码会被拒绝。仅实现原 `embed` 的 backend 继续使用原行为和空间身份。`embed` 仍是兼容契约的一部分；角色方法只提供扩展接缝，不代表任意模型已接入或通过准入。自定义 backend 必须为所有影响向量含义的模型、预处理和实现版本选择不同空间身份，系统无法推断其实际实现。

Core 0.1.56 起，对称 backend 不能使用 `asymmetric-` 编码前缀：缺少两种角色方法时在索引和查询前拒绝，防止它与声明相同身份的非对称 backend 共用空间。这会拒绝过去错误使用该保留前缀的自定义对称 backend；改为明确的对称编码身份并重建索引。若历史上曾以同一身份混用两种 backend，既有派生向量无法自动判断来源，须为正确实现选择新的编码版本并重建；不会删除原文或声称自动修复旧空间。该检查不能识别两个自定义实现故意声明相同身份但实际行为不同的情况。

#186 合成回归先复现旧代码接受冒用身份；修复后源码语义与 provider 测试 32 项、非 editable wheel 定向 4 项及 strict mypy 通过。没有接入新模型，也没有变更冻结评分或日用门禁。

`refresh(access, limit=16, timeout=30)` 每次最多处理 16 个缺失片段，分批续建，完整消息覆盖才记入 `indexed_messages`。确定性的逐片段失败（如 token 上限）记录为该空间的显式失败行 `semantic_failures_v1`（绑定状态/内容版本，`error_code='token_limit'`），计入覆盖并从待处理选择中排除；来源或内容版本变化后自动重试，结果经 `status().failed_chunks` 报告。批量/环境性失败（超时、依赖缺失）仍整体抛错、不写失败行。来源快照在短事务读取，推理在事务外进行，向量与本批覆盖元数据在同一事务提交；提交前复核来源状态版本。中断后重试只补缺失片段；抑制/更正期间的旧计算不能提交。

`status(access)` 返回当前配置空间的 `disabled/not_built/behind/ready/unavailable`、来源/完整索引消息数、待处理消息、来源水位及 `failed_chunks`（仍可召回且版本匹配的显式失败片段数）。元数据不可用时未知计数为 `null`，`error_code` 保留固定失败码，字面路径仍可查询。水位/ready 仅描述该派生空间覆盖，不能解释为抽取、事实或整体记忆处理完成。保护占位投影不送模型、不计入可向量化原文；普通原文/投影仍由原契约保管。

派生表为 `semantic_spaces_v1/semantic_documents_v1/semantic_chunks_v1`，仍在同一 SQLite，核心原文 schema 保持 v4。不同空间可并存，不自动迁移或混搜；当前没有旧空间垃圾清理或原地热切换服务。普通归档会包含这些派生表的快照，仍未加密且仅受审查恢复，不恢复执行身份或开放普通召回。

## 召回、失败和覆盖

#81 新增 status.stored_vectors：配置空间实际保存的向量行数，包含尚未清理的旧来源版本，不冒充当前可检索覆盖。已配置但尚未建表为0；未配置或存储不可用为null。indexed_messages仍表示已完成覆盖的消息数，多片段消息未完成时两者可以明显不同。

`recall(access, query, limit=10, as_of=None, recent_days=7, recent_target_chars=8192, min_similarity=0.4, timeout=30)`：先沿核心验证查询与敏感信号，再取得全历史精确/词法、近期词法和可用语义候选；每路最多 100 个候选，按消息去重，用 RRF `k=60` 融合，再应用近期保留位置。结果包含 `SearchHit` 原始来源与 code point 片段、通道、RRF 分数及可用 cosine 相似度。相似度不是事实置信度，默认 0.4 只是可调整候选阈值，未作为正式质量门槛验收。

精确检索在统一快照中为每个合格片段计算一次 cosine，再为全局和近期各自取最多 100 个来源。`semantic_scanned_chunks` 在成功时统计计算距离的片段数。覆盖和降级状态见[长期召回契约](long-term-recall-v1.md)。

来源资格在候选计算前检查，返回前再次核对当前可见性/版本；不缓存查询结果。候选是未受信任证据，不能提升为系统指令、用户授权或已确认人设。embedding 输入仍遵守普通敏感分流，模型不会解密受保护原件。

模型缺失/篡改/超时、向量错误、未建/滞后空间均返回可用词法/精确证据和 `degraded` 原因；没有云端 fallback。`evidence_state` 为 `candidates`，或没有候选时的 `incomplete/no_matching_evidence`；后者只表示当前已执行路径/阈值没有匹配，不证明开放世界中事实不存在。

单个 Node 子进程调用有 `timeout` 上限（最大 120 秒），超时由 Python 终止并回收；模型文件校验、词法扫描、融合和传输不包含在该超时内。因此它不是 T16 的端到端普通/深入查询预算，也不满足尚未验收的约 1 秒目标。当前每次调用重新载入本地模型，未引入常驻服务或跨入口模型缓存。

## CLI 与 MCP

在 checkout 外保存可信配置，例如 `E:\evernook-lab\embedding-candidate.json`（以下路径仅为示例）：

```json
{"schema_version":1,"node":"E:\\node\\node.exe","transformers_dir":"E:\\evernook-deps\\node_modules\\@huggingface\\transformers","model_dir":"E:\\evernook-models\\embedding"}
```

也可将 `schema_version` 设为 `2` 并添加 `"provider":"local-minilm"`，其余字段相同；两种配置选择同一个固定模型空间。服务、维护、MCP 和 CLI 共用内置 provider 选择器，未知 provider 会被拒绝，配置不能指定可执行模块或转发到云端。目前只有这一种实际 embedding provider；其他模型须分别实现并完成质量和资源验收，见 #124。

三个路径必须为绝对路径，模型和配置留在 checkout 外；`transformers_dir` 指向可信的固定版本安装目录，其中代码会由 Node 执行，完整性属于本地依赖安装边界。当前验证复用了 R0 [固定依赖与准备说明](../../probes/core-compare/README.md)，不在请求期间执行安装、下载或 npm 脚本。

```text
evernook refresh-semantic --config E:\EverNookData\runtime.json --access E:\EverNookData\mcp-access-owner.json --embedding-config E:\evernook-lab\embedding-candidate.json
evernook semantic-status --config E:\EverNookData\runtime.json --access E:\EverNookData\mcp-access-owner.json --embedding-config E:\evernook-lab\embedding-candidate.json
```

维护端根据 `pending_messages` 重复刷新；不会自动启动后台消费者。`evernook recall` 使用同样路径参数，stdin 单个 JSON 对象如 `{"query":"饮食偏好","recent_days":30,"recent_target_chars":16000}`。未提供模型配置仍能得到词法候选和 `semantic_disabled`。其他 CLI 规则见 [CLI v1](cli-v1.md)。

MCP 启动参数可选加 `--embedding-config <absolute-path>`，新增查询工具 `evernook_v1_recall`，参数同上；它不能从工具参数修改模型路径或维护索引。默认 `recall` profile 现在共七个查询工具，`trusted-host` 共十一项，原有四项受限操作仍只在可信宿主模式开放。`status.semantic` 给出配置空间的覆盖；旧 `search` 仍只提供原词法/精确契约。

Node worker 设置本地模型模式、禁用模型文件缓存，拒绝 Node fetch/socket；成功响应必须报告零次被拦截的出网尝试。这是进程层限制，不等于用户系统已断网或所有原生库网络能力都经过 OS 隔离。没有改变系统网络配置，完整 OS 断网、正式中文质量集及规模时延仍待准入。

## 可选语义覆盖检查点

具有 `maintain` 权限的身份可以为一个嵌入空间构建覆盖检查点。新库默认关闭。建好语义表后，可在维护窗口执行以下命令。准备或重建会占用 SQLite 写事务。对另一个模型执行准备会切换检查点跟踪的空间。

```powershell
evernook prepare-semantic-checkpoint --config $runtimeConfig --access $clientFile
evernook refresh-semantic-checkpoint --config $runtimeConfig --access $clientFile --limit 5000
evernook rebuild-semantic-checkpoint --config $runtimeConfig --access $clientFile
evernook remove-semantic-checkpoint --config $runtimeConfig --access $clientFile
```

以上为已激活独立服务的用法。准备、重建、刷新都使用服务启动时绑定的嵌入空间，服务拒收调用方指定的模型配置路径。已变更的模型配置需要重启，换空间后应执行准备，再继续刷新。服务已停用模型或绑定配置损坏时，维护身份仍可移除检查点。四项操作仅通过可信 CLI/RPC 维护入口开放。尚未激活服务的直接模式需要为准备和重建命令添加 `--embedding-config $modelConfig`。

Python 入口为 `SemanticRecall.configure_coverage_checkpoint` 和 `refresh_coverage_checkpoint`。检查点保存来源 ID、接收时间、版本覆盖状态、有效失败分块数及物理向量数。消息、可见性、采集清单及当前空间的文档、分块和失败记录变动时，SQLite 触发器在同一事务记录待复核 ID。触发器写入失败会回滚来源事务。`semantic-status` 和召回在同一状态读取快照中复核最多 100 个变动来源；读端保留队列。已存向量数包含该空间中已失效或已被抑制来源的遗留向量。队列为空时，状态查询读取受校验的计数与接收时间索引。所有候选仍按原流程检索和验证。

超过 100 个待复核 ID 时使用原有全量统计，维护端每次可消化 1–5,000 个。检查点缺失、触发器不全、数据库结构版本变化、摘要失配、未授权改写派生行、查询其他空间或旧库缺少失败记录表时，也沿用全量路径。重新执行准备可重建失效检查点；旧库可先通过已有 `refresh-semantic` 补齐语义表。遇到同名异构对象时，维护操作报告 `semantic_checkpoint_conflict`。普通归档剥离检查点，确认彻底删除时同步清除相应 ID。

## 可选 int8 候选筛选

`prepare-vector-screen` 从指定嵌入空间已有的 float32 分块构建紧凑编码。默认关闭。在已激活服务上，四项命令都使用可信 CLI/RPC，要求 `maintain` 权限；准备、重建与刷新使用服务绑定的模型。直接 SDK 可以调用 `SemanticRecall.configure_vector_screen` 和 `refresh_vector_screen`。机器学习模型工具没有这些维护操作。

```powershell
evernook prepare-vector-screen --config $runtimeConfig --access $clientFile
evernook refresh-vector-screen --config $runtimeConfig --access $clientFile --limit 5000
evernook rebuild-vector-screen --config $runtimeConfig --access $clientFile
evernook remove-vector-screen --config $runtimeConfig --access $clientFile
```

直接 CLI 模式准备或重建时另传 `--embedding-config $modelConfig`。一次跟踪一个空间；切换空间需显式重建。准备、重建和待更新来源刷新都占用写事务。后台语义写入在同一事务登记变化，普通召回会把少量变化来源直接纳入精确计算；维护者可按批刷新。派生表、触发器或模式保护失效时执行准备以重建。删除来源时同时清理该来源的编码和队列，普通归档剥离表及触发器；删除索引后 SQLite 可复用空闲页。

启用后，仅固定的 Windows x64 sqlite-vec 0.1.9 原生后端对正相似度阈值使用此筛选。粗分加上查询、存储编码和原生浮点舍入的保守误差界能达到阈值时，取该来源所有合格分块，调用原有 float32 cosine。资格、源版本、全局和近期通道各自的排序及引用跨度在同一快照核验。此路径报告 `sqlite-vec-0.1.9-int8-bound-exact`，`semantic_scanned_chunks` 统计实际接受 float32 评分的合格片段。

明确给出起止时间、筛选结果超过 4,096 个分块、超过 100 个变动来源或变动分块、存在未能安全编码的分块、原生后端不可用、缓存空间或完整性校验失败，均回退到完整精确查询。超过候选上限时，筛选会增加回退成本。公开接口和实际有语义命中的量测见[百万条误差界对照](../reports/2026-11-26-vector-screen.md)。
