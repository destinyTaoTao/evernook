# 当前实现与架构图

更新：2026-10-30，代码基线 `ab17a5d` 加本轮 [T15 #16](https://github.com/destinyTaoTao/evernook/issues/16) 显式语义召回增量。本页依据源码与契约描述已有能力；[目标架构](architecture.md)和[验收路线](roadmap.md)中的后续设计不代表已经实现。

## 系统结构

当前交付是 Python 本地库、可信[本地 CLI](contracts/cli-v1.md)与可选 stdio MCP 适配器。不同实例可读取同一配置和 SQLite 账本，但尚无已部署的共享守护服务、HTTP 监听或日常宿主自动采集链路。核心/CLI 无第三方运行依赖；MCP 为可选依赖，默认只开放七个查询工具，可信启动端显式选择 `trusted-host` 才开放完整十一项。

```mermaid
flowchart TB
    Python[可信本机 Python 调用端] --> Runtime[MemoryRuntime 核心]
    Python --> Candidates[CandidateWorkflow 显式任务与候选]
    Candidates --> Runtime
    Client[已配对 MCP 调用端] -->|stdio| MCP[mcp_server.py 协议适配]
    MCP --> Runtime
    Runtime --> Auth[身份 / 契约 / 来源与状态检查]
    Auth --> Safety[safety.py 敏感分流]
    Safety --> Ledger[(memory.sqlite3 原文与安全投影)]
    Runtime --> Lexical[lexical.py 归一化与匹配]
    Lexical --> Index[(同库 FTS5 派生索引)]
    Index --> Ledger
    Ledger --> State[重要交代 / 更正与抑制 / 来源片段]
    Ledger --> Jobs[原始待处理任务：无自动消费者]
    Candidates --> Ledger
    Runtime --> Archive[archive.py 普通归档与审查恢复]
    Archive --> Review[EvidenceArchive：review_required]
    Files[仓库外配置 / 访问材料 / HMAC 密钥] --> Auth
    Paths[paths.py 本机 NTFS / 目录 ACL] --> Files
    Future[规划：dsh 日常适配 / pi / 桌宠] -. 待接入 .-> Runtime
    classDef planned fill:#fff7ed,stroke:#9a3412,stroke-dasharray:5 5
    class Future planned
```

实线表示已存在的调用或数据关联，虚线表示规划接入；图中各模块不是独立服务。配置、访问材料、数据库、归档和模型资产均位于 checkout 外。ACL 防止普通其他账户直接访问文件，不隔离同账户恶意代码或管理员。

| 源码 | 当前职责 |
|---|---|
| [runtime.py](../src/evernook/runtime.py) | 配置/配对、鉴权、SQLite 事务、幂等、原文/附件、重要交代、更正/抑制与词法维护入口 |
| [models.py](../src/evernook/models.py) | 版本化输入、来源、回执、状态、分页和检索结果 |
| [safety.py](../src/evernook/safety.py)、[paths.py](../src/evernook/paths.py) | 有限凭证信号检测，以及受支持文件系统与运行目录权限检查 |
| [lexical.py](../src/evernook/lexical.py) | Unicode 归一化、中文 1/2/3 字 gram、安全词项与原文坐标映射 |
| [cli.py](../src/evernook/cli.py) | 可信本地初始化/配对、stdin JSON 保存与检索、指定回取、状态及词法维护；无 MCP 依赖 |
| [mcp_server.py](../src/evernook/mcp_server.py) | 默认七个查询工具，可信宿主模式十一项；启动配置限制工具权限，固定失败回执，复用核心检查 |
| [semantic.py](../src/evernook/semantic.py)、[embedding.py](../src/evernook/embedding.py) | 显式本地模型空间与可重建向量、RRF 融合、自适应近期范围、来源过滤与降级；不自动启用模型 |
| [archive.py](../src/evernook/archive.py) | 一致性普通快照、清单/哈希校验、恢复发布与只读归档审查 |
| [candidates.py](../src/evernook/candidates.py) | 显式任务租约/重试、来源绑定的候选批次提交与失效过滤；不运行模型或接纳事实 |
| [tests/contracts](../tests/contracts) | 公开边界的合成回归，含真实 stdio、并发与进程退出场景 |
| [probes](../probes) | R0 宿主、模型和加密原语实验，不作为正式适配器或启用状态 |

## 写入与保存确认

```mermaid
sequenceDiagram
    participant Caller as 已授权调用端
    participant Core as MemoryRuntime
    participant DB as SQLite WAL / FULL
    Caller->>Core: submit 或 submit_capture（稳定来源与内容）
    Core->>Core: 身份/版本/来源/输入校验
    Core->>Core: 敏感分流与受密钥保护的重试指纹
    Core->>DB: BEGIN IMMEDIATE；查询来源身份
    alt 相同来源、相同请求
        DB-->>Core: 已有稳定回执
        Core-->>Caller: 原回执，不重复追加
    else 相同来源、不同请求
        Core-->>Caller: source_conflict；事务退出
    else 新来源
        Core->>DB: 消息与待处理任务（采集路径含附件/清单）
        Core->>DB: COMMIT
        DB-->>Core: 提交完成
        Core-->>Caller: committed 或 projection_only
    end
```

来源为入口、会话、分支、源事件 ID 的组合；相同文本的不同真实事件分别保留。SQLite 提交失败不能返回成功。核心使用短事务，尚无统一后台写调度器或自动抽取消费者；候选任务需由可信本地维护端显式领取/提交。

保险库关闭时，声明或检测为敏感的内容只保存投影，并明确 `original_saved=false`；有限检测不保证任意秘密均可识别。重要标记、更正/抑制是各自显式操作，不应从上图推断一次普通提交会自动完成它们。

工具与有界文本附件保存在同一数据库事务内，缺失、超限、不支持和未完成内容报告采集缺口。当前接口不实现持续上传或逐 token 显示。dsh 的显示前持久化屏障仍是固定内核探针，实际展示通道、重试与恢复回放尚未验收。契约见[原文](contracts/python-text-v1.md)、[采集](contracts/capture-v1.md)、[重要交代](contracts/important-v1.md)与[显式变更](contracts/changes-v1.md)。

## 当前检索与降级

```mermaid
flowchart TD
    Query[exact 短语 / lexical 空白分隔词] --> Check[身份、查询、敏感信号与时间范围检查]
    Check --> State[读取当前可召回状态与索引覆盖]
    State --> Ready{派生索引状态}
    Ready -->|ready| FTS[安全 FTS5 候选]
    Ready -->|behind| Both[已有候选 + 扫描未索引原文]
    Ready -->|not_built / unavailable| Scan[扫描当前原文或投影]
    FTS --> Match[回源校验与归一化字面匹配]
    Both --> Match
    Scan --> Match
    Match --> Evidence[来源、版本、原文 code point 片段]
    Evidence --> Result[分页结果 + 覆盖水位 + 降级原因]
    Manual[本地显式 refresh / rebuild] --> Index[可重建 FTS5 索引]
    Index --> FTS
```

`exact` 对整个短语匹配，`lexical` 对空白分隔词执行 AND；两者均使用派生归一化表示，返回原件或投影对应的坐标。查询文本按字面处理，不执行用户的 FTS 语法。

普通读取和候选路径过滤已更正、撤销或抑制的消息；索引维护提交前重新核验来源状态。索引落后时补扫未处理原文；索引失败时扫描原文，明确报告覆盖缺口。水位是最大接收时间，不能代替连续完成证明。原文数据库失败仍报存储错误，不伪造空结果。

独立附件内容尚未进入全文检索。时间过滤依据已有事件时间，不具备自然语言历史事实推断。新增[显式语义召回](contracts/semantic-v1.md)将固定本地 embedding 候选与字面证据融合，近期窗口可按天数与内容密度扩展，同时保留全历史路径；没有重排、已接纳事实或最终上下文预算构造。模型候选与小样本验证不等于正式质量准入。详见[词法契约](contracts/lexical-v1.md)。

## 候选任务与来源校验

```mermaid
flowchart TD
    Operator[可信本地维护端] --> Begin[领取来源范围 / 处理版本 / 租约]
    Begin --> Input[已提交的来源与状态版本快照]
    Input --> Outside[事务外由调用端提供候选]
    Model[真实离线模型：未接入] -. 后续准入 .-> Outside
    Outside --> Check[身份 / 租约 / 来源版本 / 原文片段 / 角色检查]
    Check --> Save[(全部候选 + 成功状态同事务提交)]
    Save --> Read[读取时重新校验整批来源]
    Read --> Valid{所有来源仍有效}
    Valid -->|是| Candidate[仅 candidate / disputed，不进入普通召回]
    Valid -->|否| Stale[stale / 空候选 / 无完成水位]
    classDef planned fill:#fff7ed,stroke:#9a3412,stroke-dasharray:5 5
    class Model planned
```

同范围/来源版本/处理版本幂等；并发只有一个有效租约，失败或租约过期后以新令牌重领。令牌只返回领取端，数据库只留 HMAC 摘要。任务成功、候选和可见完成水位一致；水位只针对本批输入，不代表更早记录均已处理。原始 jobs 仍为 pending，尚无自动任务选择或消费。

候选必须匹配来源表示、版本和 code point 片段；模型置信度没有接纳权限。角色校验和声明的引文/扮演分类不等于语义判真，所有结果都保持 candidate/disputed。任一批次来源更正、撤销或抑制后，整批保守失效。没有自动事实接纳、历史有效期判断或真实模型调用。详见[候选契约](contracts/candidates-v1.md)。

## 普通导出与受控恢复

```mermaid
flowchart TD
    Live[(运行时普通账本)] --> Grant[身份与 archive_review 权限]
    Grant --> Snapshot[SQLite Backup API 一致性快照]
    Snapshot --> Stage[私有暂存：数据库 + manifest]
    Stage --> Verify[版本 / SHA-256 / 结构 / 外键 / 附件校验]
    Verify --> Publish[目录重命名发布普通归档]
    Publish --> Copy[新目录复制并重新校验]
    Copy --> Review[EvidenceArchive 只读审查]
    Review --> Gate[始终 review_required / archive_only]
    Gate -. 待实现准入 .-> Future[最新控制记录重放 + 运行时恢复]
    Secrets[排除材料：配置 / 令牌 / HMAC 密钥]
```

普通归档是未加密的用户证据，包含已失效原档供明确审查；它不是可自动接管的运行时备份。新目录恢复不生成配置或密钥，不提供普通 `read/search/submit`。无法证明快照之后的更正/抑制/删除完整，因此没有把恢复副本转为可召回状态的接口。图中虚线仅表示后续准入缺口，排除材料不进入归档。详见[归档契约](contracts/archive-v1.md)和[实施验证](reports/r1-export-2026-10-30.md)。

## 实现进度与阶段缺口

| 阶段/能力 | 当前事实 | 尚未完成 |
|---|---|---|
| R0 验证与选型 | 已有 dsh、MemOS 对照和凭证原语探针；原文账本路线已记录 | 全面 OS 断网、正式模型质量、实际宿主集成等准入 |
| R1 可靠底座 | 原文、重要交代、显式更正/抑制、有限附件、普通归档与审查恢复已实现 | 加密备份、目的地/恢复材料、调度/保留、最新控制记录重放、运行时恢复与 RPO/RTO |
| R2 事实与时态 | 有显式可恢复任务、多来源候选/争议标记、来源片段校验和失效过滤 | 真实模型自动抽取/语义分类、事实接纳/合并、纠错与现实变化区分、历史有效期 |
| R3 召回与沉淀 | 中文词法/精确路径、显式本地语义候选、融合与自适应近期范围已实现；维护按批显式执行 | 模型正式准入、预算上下文、日周沉淀、维护调度、规模向量检索与模型换代 |
| R4 跨入口与长期运行 | 真实 stdio 协议和两个配对身份共用账本已有回归 | dsh 日常适配、pi、桌宠、安装升级与长期运行验收 |
| 独立安全与质量 | 普通路径分流、ACL 和契约回归已有实现 | 凭证保险库/逐次明文授权、彻底删除、正式质量集与十万/百万规模性能验收 |

MCP 的可信宿主模式有 11 个工具，覆盖文本保存/读取、来源/时间列表、搜索、重要标记/回取、更正、状态与能力；默认查询模式只开放七项，并在直接调用时拒绝其余操作。候选工作流、采集附件、配对、词法维护、归档审查和导出目前均不作为 MCP 工具开放。协议回归通过不表示调用端已经配置、自动采集或注入记忆。

归档历史验证见[归档报告](reports/r1-export-2026-10-30.md)，本轮任务与候选验证见[候选报告](reports/r2-candidates-2026-10-30.md)。本页不将测试数量、R0 小样本耗时或约 1 秒目标解释为生产质量/性能验收。

## 后续推进边界

T09 仍开放，备份按已确认决定继续待办；[T10](https://github.com/destinyTaoTao/evernook/issues/11) 的日常 dsh 链路相关依赖未解除。自动抽取、语义模型正式启用及其他任务需要分别落实模型、质量和资源准入。当前页补充实现地图，不改变这些产品决定，也不授权部署、启用或自动执行后续任务。
