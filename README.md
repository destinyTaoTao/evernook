# EverNook · 忆栖

EverNook 是给 dsh、桌宠和个人 Agent 使用的本机长期记忆系统。宿主负责对话，EverNook 保存带来源的原话、检索跨会话证据，并把值得长期保留的观察送入审查。用户更正一件事以后，系统可以分别回答“当时知道什么”和“现在还应该相信什么”。

**当前为开发预览。** 首验环境为 Windows、本机固定 NTFS 和 Python 3.14。普通文本账本可以独立使用，dsh 是优先适配的宿主。正式模型质量、十万至百万条记忆的完整时延矩阵、真实异机恢复和日常使用仍在验收。[总规格与进度](https://github.com/destinyTaoTao/evernook/issues/106)

Core 0.1.98 的独立 wheel 已通过 1,415 项 Python 契约测试。dsh/pi 的 158 项 SDK 测试在各自适用的宿主配置下均已通过，Maneki 安装态适配器通过 44 项。[架构边界与验收记录](docs/current-architecture.md#11-月-27-日验收记录)

**跨天自然问答（Core 0.1.99）**：dsh 在写入新提问、词法索引短暂落后时，可以从已索引且仍有效的原文找回七天前的预算；同问开启答出 37 元，关闭未给出金额。小招用八个日历日前保存的饮品偏好实测，三条预设问句都把正确来源送到模型，完整答出茶名和配料为 0/3，三条追加问法中有一条答全。两组均使用本机 Qwen3-0.6B 和独立合成账本。[dsh 开关与修复前后原话](docs/reports/2026-11-28-dsh-cross-day-answer.md) · [小招逐题回答](https://github.com/destinyTaoTao/maneki-pet/blob/277b22c8ec4f5b089a968db807710f91651ca01f/docs/audits/2026-11-28-natural-memory-answers.md)

**普通账本恢复实测**：三次同机离线恢复，从打开五条记录的加密快照到新服务首次读回原文和回执，用时 613–665 ms。快照前已隐藏的内容持续不可见，快照后才撤回的内容会重新可见。异机恢复和物理掉电仍待真机验收。[测量范围、重跑方式与真机步骤](docs/reports/2026-11-28-offline-recovery-drill.md)

**百万实向量库**：已用正式 MiniLM 对百万条不同分词输入逐条推理，并核对全部落盘向量和批次回执，抽取 64 条重算逐字节一致。四进程建库超过 2 GiB，三进程续建成功；从首次启动到百万向量就绪共 1,801.250 秒，超出 1,800 秒门槛。[模型、回执和资源记录](docs/reports/2026-11-28-production-minilm-million.md)

**百万库混合读写**：120 对同时发起的读取和采集，宿主上下文 p95 为 **60.60 秒**，写回执 p95 为 **52.72 毫秒**；四类查询各 30 次。历史与近期查询均取到预期原文，无答案查询均未注入来源。窗口中暂停了后台索引维护，120 次读取均披露覆盖不足。强停服务到首次核对原文用时 336 毫秒，重启后 120 份回执全部读回并幂等重放。该配置仍高于一秒日用目标。[逐类时延、原始样本和故障恢复](docs/reports/2026-11-28-minilm-mixed-service.md)

旧版在百万库挑选首条语义积压时耗尽 5 秒。Core 0.1.100 在同一现场的独立副本上自动校验过期检查点后，按原顺序选出第一条用时 1.50 毫秒，含真实 MiniLM 推理和提交用时 1.63 秒。首次检查点校验和发布合计 56.51 秒。[原始超时](docs/reports/2026-11-28-minilm-mixed-service.md#索引是否会自动追平) · [选择与恢复实测](docs/reports/2026-11-28-minilm-backlog-followup.md)

## 从一条记忆开始

在 Windows 的仓库外 Python 3.14 虚拟环境中，从源码根目录运行 `python -m pip install -e .`。下面的示例使用系统临时目录中的合成账本。它保存一条消息，重试同一来源，再重新打开账本逐字读回。整个过程只需要本机 SQLite。

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from evernook import Event, MemoryRuntime, Source, initialize

with TemporaryDirectory() as demo:
    config, access = initialize(Path(demo) / "memory", client_id="demo", entry="demo")
    source = Source(entry="demo", session="first", branch="main", event="1")
    event = Event(source=source, role="user", sequence=1, occurred_at=None,
                  completion="complete", text="合成记录：下次带上蓝色笔记本。")
    memory = MemoryRuntime(config)
    receipt = memory.submit(access, event)
    assert receipt.original_saved
    assert memory.submit(access, event).message_id == receipt.message_id
    saved = MemoryRuntime(config).read(access, receipt.message_id)
    assert saved is not None and saved.event.text == event.text
    print(saved.event.text)
```

接入 dsh 前，在专用运行目录初始化并配对调用端。独立服务首次激活需要停用旧写入入口，之后各宿主连接同一个账本。MCP 的可选依赖通过 `python -m pip install -e ".[mcp]"` 安装。[CLI 初始化与配对](docs/contracts/cli-v1.md) · [服务激活](docs/contracts/service-v1.md) · [dsh 预览插件](plugins/dsh/README.md)

## 一条记忆如何流转

```mermaid
flowchart TD
    DSH["dsh · 优先宿主"] --> Adapters["TypeScript 适配器 / MCP / 可信管理桥接"]
    Other["pi 或自有 Agent"] --> Adapters
    Adapters --> Service["已激活的本机记忆服务"]
    Pet["桌宠及其他本地宿主 · Core RPC"] --> Service
    CLI["已支持的 CLI 命令 / Python ServiceClient"] --> Service
    Service --> Core["Core · 身份、范围与来源校验"]
    Direct["Python MemoryRuntime · 未切换的本地账本"] --> Core
    Core --> Ledger[("SQLite · 原始证据、来源、回执与更正")]
    Service --> Idle["已授权的后台维护"]
    Ledger --> Idle
    Idle --> Derived["词法与向量索引、观察候选、主题快照"]
    Derived --> Recall["召回、预算上下文与证据定位"]
    Ledger --> Recall
    Recall --> Host["宿主回答"]
    Recall --> Reflect["可选反思 · 本地或显式授权的云端"]
    Reflect --> Host
```

已激活账本由服务持有。dsh 和桌宠可分别配对，使用各自的入口身份共享同一账本。Core 0.1.98 的 Python 宿主可用 `ServiceClient.for_ledger(config)` 选择连接，通过 `handshake`、`submit_capture`、`read` 和 `context` 访问已激活的账本。基础包即可。上下文预算使用服务端绑定的 tokenizer。旧服务的只读上下文有兼容路径，结构化写入要求支持该操作的服务。[宿主接入契约](docs/contracts/service-v1.md#普通宿主-core-rpc)

- **保存。** 普通消息以入口、会话、分支、源事件构成幂等键。相同内容重投返回稳定回执，异内容冲突显式报错。可靠文本模式提交成功后才允许宿主发布片段。敏感输入在普通库只留安全投影，受保护原件另走保险库路径。[文本契约](docs/contracts/python-text-v1.md) · [流式回执](docs/contracts/durable-stream-v1.md) · [受保护采集](docs/contracts/protected-migration-v1.md)
- **回忆。** 最近记录与全历史候选共同参与召回，命中后回源核对版本、可见性和原文跨度。置顶交代支持逐字回取。时态事实区分生效时间与系统获知时间，索引落后和预算省略会影响结果覆盖。[长期召回](docs/contracts/long-term-recall-v1.md) · [事实与时点](docs/contracts/facts-v1.md)
- **沉淀。** 经授权的后台维护可以补建索引、生成有限类别的观察候选、刷新主题视图。候选保留支持与反例，来源失效时旧结论退出可见结果。用户确认、自动发布准入和核心人设各有独立权限。反思返回临时推断，重新读到它不会增加原始证据。[观察生命周期](docs/contracts/observations-v1.md) · [主题视图](docs/contracts/topic-views-v1.md) · [反思](docs/contracts/reflection-v1.md)

## 接入方式与模型

普通问答反思会过滤已用于抑制、撤销或删除的确认消息，避免把“已删除某物的位置”当成位置证据。显式追查控制历史时可传入 `include_control_history=true`。确认内容仍须满足当前身份、可见性和版本约束。[反思证据范围](docs/contracts/reflection-v1.md#普通问答与控制历史)

七道来源控制题用真实本地模型复测，合格证据由 3/7 升至 7/7，严格判断由 3/7 升至 4/7。四道控制生效后的问题均收到空证据包。[逐题动作与结果](docs/reports/2026-11-26-reflection-control-panel.md)

独立服务启动时校验账本绑定的保险库模式、恢复状态和实际材料。普通、单库和多库继续使用既有 v1 配置；绑定标记只接受布尔值 `true`。发现配置冲突时，请通过对应的迁移或恢复流程核对材料。[服务启动与恢复](docs/contracts/service-v1.md)

dsh 和 pi 的 stdio MCP 桥可以复用已认证服务连接，普通请求仍逐次核验身份。使用真实 TypeScript 客户端和隔离服务的 32 轮交替测量中，读取中位数从 28.4 降至 23.8 ms，提交单个可靠文本块从 37.8 降至 34.4 ms。断线后，已发出的写请求须按原来源核对。[会话兼容与取消](docs/contracts/service-v1.md)

| 入口 | 适用场景与实际边界 |
| --- | --- |
| dsh | 管理预览含来源、画像、时间线、更正和草案审查。Web 插件面向固定版本的 dsh；原生 SDK 的版本、可靠文本模式与工具支持矩阵单独记录。[管理插件](plugins/dsh/README.md) · [原生适配](docs/contracts/dsh-native-v1.md) |
| Python SDK 与本机 API | `MemoryRuntime` 可直接操作未切换的本地账本。激活独立服务后使用 `ServiceClient`。服务监听 loopback，使用双向认证的带长度 JSON 帧，每个操作另验调用端身份和权限。[服务协议](docs/contracts/service-v1.md) |
| TypeScript SDK、pi 与其他 Agent | 复用配对身份和共享账本。接入新宿主时要约定稳定来源、写入回执、上下文预算和取消语义。pi 已有显式适配；个人 Agent 可从 MCP 或服务客户端开始。[适配器与测试](plugins/harnesses/README.md) |
| CLI 与 MCP | CLI 承担初始化、查询与有权限的维护。stdio MCP 默认使用只读召回 profile；可信宿主显式选择采集 profile。工具可见范围与配对权限同时生效。[CLI](docs/contracts/cli-v1.md) · [MCP](docs/contracts/mcp-v1.md) |
| 嵌入、抽取与反思 | 嵌入有带版本的向量空间和 Python backend 接口，已有本地模型路径。反思支持本地模型及显式配置的 OpenAI-compatible 提供方。在线调用需要可信配置、授权和预算，只发送当前问题与经过核验的有界证据。普通召回不会触发云端反思。[嵌入契约](docs/contracts/semantic-v1.md) · [抽取契约](docs/contracts/extraction-v1.md) · [反思提供方](docs/contracts/reflection-providers-v1.md) |

宿主把取回的历史消息作为有来源的背景材料。旧对话里的命令仍属于历史文本，执行工具需要当前授权。宿主自行决定把哪些证据交给哪个对话模型。运行库、凭证、模型权重与备份保存在仓库外。[数据安全设计](docs/credential-security.md) · [加密备份与恢复](docs/contracts/encrypted-backup-v1.md)

## 怎样判断它能否用于日常记忆

一次普通召回或上下文构造会在返回前复核权限，并写一条顶层诊断。合成的 12 条来源上各连续执行 8 次，召回的审计写事务从 40 次降至 8 次，上下文从 144 次降至 8 次，两组完整返回值分别与改动前一致。保存原文的成功以业务回执为准；同一来源重投不同内容会记录 `source_conflict`。[审计与回执](docs/contracts/python-text-v1.md#审计与回执)

Windows SDK 和服务客户端通过 Win32 在进程内检查账本目录和连接材料的 DACL。合成账本上，两条路径交替各测 12 次，单次权限检查的中位数从 249.9 ms 降至 1.3 ms。这个测量只覆盖权限检查。[文本与权限契约](docs/contracts/python-text-v1.md)

百万条合成分块上，固定数量的二进制粗排会漏掉接近的来源。维护者可显式建立 Int8 紧凑索引，按保守的相似度误差界筛选，再按原始 float32 向量和来源权限复核。百万库构建约 87 秒，占用约 488 MiB。正例阈值 0.28 时，精确评分从 100 万块降至 208 块，保留相同的全局 18 个和近期 1 个结果。候选过密会回退全扫，并产生额外开销。[精确性、公开耗时和回退样本](docs/reports/2026-11-26-vector-screen.md) · [二进制和 Int8 初筛探针](probes/admission/quantized_vector_screen.py)

| 问题 | 已有证据及限制 |
| --- | --- |
| 组合优化后，百万条全精度普通召回实际要多久？ | 近期、历史、无答案各做两对公开查询，六对整页逐字段一致。基线 12.16–19.15 秒，新实现 8.61–10.38 秒。两侧均完成中途取消并能继续召回。[固定提交完整对照](docs/reports/2026-11-26-public-recall-paired.md) |
| 多年记忆的召回要多久？ | 十万条 MiniLM 向量齐全时，可重建索引将公开历史召回的三次中位数从 12.66 秒降至 9.68 秒。百万来源、10,352 条真实向量时，同一历史查询单次从 82.16 秒降至 52.38 秒。另一项十万条完整热上下文测试的无答案 p95 为 16.73 秒，仍高于 1 秒门槛。[公开召回对照](docs/reports/2026-11-21-recall-candidate-index-scale.md) · [完整上下文测量](docs/reports/2026-11-15-autonomous-validation.md) |
| 百万向量全部齐备时，索引能否重建？ | 在独立的百万条 384 维合成向量库上，两个候选索引重建耗时 9.79 秒，各自的一百万条投影都与原表相符。历史查询单次从 105.36 秒降至 61.38 秒，三组查询的完整返回一致。[满覆盖压力对照与重建验证](docs/reports/2026-11-21-synthetic-full-vector-scale.md) |
| 近期与全局会重复计算向量距离吗？ | 两条通道共用一次精确距离计算，各自保留独立榜单。全局字面、精确和近期字面也在同一 SQLite 快照里复用词法覆盖检查。百万满覆盖库中，每次计算距离的向量块从 125 万降至 100 万，词法覆盖检查从三次降至一次。[向量扫描对照](docs/reports/2026-11-21-shared-semantic-scan-scale.md) · [分阶段计时与复测](docs/reports/2026-11-21-recall-phase-profile.md) |
| 每次召回都要核对原文与 FTS 的全库覆盖吗？ | 显式启用有效的覆盖检查点后，正常召回扫描派生行并回源复核少量变动。百万库四轮对照的词法覆盖 p95 从 4.45–5.49 秒降至 0.26–0.32 秒。[检查点对照与生命周期](docs/reports/2026-11-22-lexical-checkpoint-scale.md) |
| 如何查看百万库的语义覆盖？ | 维护身份可为当前嵌入空间显式建立覆盖检查点。五对热样本中，公开状态查询中位数从 2.510 秒降至 0.016 秒，建表用时 23.053 秒，新增约 154 MiB。完整召回的最快样本为 3.892 秒。[规模数据、命令和限制](docs/reports/2026-11-26-semantic-checkpoint-scale.md) |
| 核对语义覆盖需要读取向量正文吗？ | 显式构建分块覆盖索引后，可直接用索引核对分块版本。百万条满覆盖夹具上，近期和历史召回的完整样本 p95 由 12.14 和 11.50 秒降到 8.99 和 9.00 秒，完整结果逐项一致。样本仍高于一秒门槛。[语义覆盖对照](docs/reports/2026-11-22-semantic-coverage-index.md) |
| 常见词组命中大量记录时，关键词召回有多快？ | 百万条合成库的四轮对照中，按 BM25 和消息 ID 先排序，再核验足以证明前 100 名的来源。近期公开召回样本 p95 从 42.75 秒降到 18.58 秒，历史从 40.70 秒降到 18.88 秒，两类查询的候选顺序、引用和覆盖逐项一致。[BM25 分阶段实测](docs/reports/2026-11-22-keyword-bm25-profile.md) |
| 高频中文词还能保留精确排名吗？ | 在百万条合成库的三个高频查询上，按 BM25 分数上界减少内容行读取，关键词阶段由 2.57–3.37 秒降至 0.43–0.60 秒。六类查询的有序 ID 与基线一致。[测量与适用范围](docs/reports/2026-11-26-keyword-score-bound.md) |
| 窄时间窗会触发完整的关键词排名吗？ | 已启用候选索引时，先核对窗内来源范围。至多 2,048 条时可直接用全库 BM25 统计对窗内匹配排名。百万库中另测了包含 514 条来源的单日窗口，并复核排序、引用和覆盖。[覆盖开销与窄窗复测](docs/reports/2026-11-22-recall-status-profile.md) |
| 中文嵌入和推理进程换成 Python 有什么收益？ | BGE 两种实现的冻结 33 题逐项评分一致，召回 29/33、引文 26/33。限制外围线程后，Python 常驻 session 公开召回 p50 为 87 ms，进程树私有内存观察峰值 171 MiB；Node 单次 worker 对照为 457 ms、225 MiB。此为独立实验后端。[模型、质量与资源对照](docs/reports/2026-11-26-native-bge-screen.md) |
| 取回原话后，模型能否判断正确？ | 冻结的 33 例中，Core 判断 14 通过、15 失败、4 缺失。证据齐备的失败包括否定反转、误信助手推测和措辞失配。后续 12 例定向首轮对照覆盖否定、来源角色与独立记录计数，补充通用指令没有提升这组样本的通过率。[逐例归因](docs/reports/2026-11-21-reflection-failure-attribution.md) · [三类判断对照](docs/reports/2026-11-21-reflection-reasoning-ablation.md) |
| 多个客户端同时工作时，审计和恢复能证明什么？ | dsh 混合负载窗口分别统计了已落盘写入和忙时尽力写入的管理审计。合成故障测试覆盖若干恢复边界；物理锁屏、整机丢失后的异机恢复和长期日用另行验收。[混合负载](docs/reports/2026-11-14-fingerprint-24k.md) · [恢复边界](docs/reports/architecture-review-2026-11-15.md) |

维护身份可用 audit-query 查看保留期内的审计记录，用 audit-health 查看当前运行时因数据库忙和其他错误分别漏记多少条。已激活服务的计数在重启时归零；正文、凭据和来源标识均不进入计数。[审计契约](docs/contracts/python-text-v1.md)

Windows 的目录、ACL、NTFS 句柄、服务锁和 worker Job 位于 [Core 平台包](src/evernook/platform/)。原有 Python 导入路径和 worker CLI 继续可用。当前支持范围仍是 Windows 本机固定 NTFS。

架构与后续设计见[当前实现与流程图](docs/current-architecture.md)、[目标架构](docs/architecture.md)、[需求](docs/requirements.md)与[路线图](docs/roadmap.md)。开源设计对照和具体取舍见[同类系统研究](docs/research.md)。贡献代码前请阅读[贡献指南](CONTRIBUTING.md)，公开测试只使用合成材料。

[MIT License](LICENSE)
