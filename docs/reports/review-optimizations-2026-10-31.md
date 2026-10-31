# 整体关键用例复审与优化（第二轮）

对应 [Issue #30](https://github.com/destinyTaoTao/evernook/issues/30) 的延续轮次。基线 `af747d4`（192 项契约全绿 + 14/14 插件套件 + mypy 51 文件），本轮按四个模块组（核心账本、检索/派生、时间/维护、安全/生命周期）以标准关键用例类别（崩溃恢复、幂等、并发、时间语义、安全边界、资源泄漏、错误码一致性、Windows/Unicode 边界）做整体复审。下述实现不代表宿主接入、保险库、备份调度或记忆质量验收。

## 复审方法与结论概览

- 四组独立审查共报告 0 个 P0、1 个 P1、16 个 P2；每个发现先在源码中人工复核确认后修复。
- 修复 1 个 P1：语义索引单个无法向量化片段会永久卡死增量构建队列。
- 其余为硬化项：一处覆盖报告语义、两处错误码一致性、三处状态/诊断语义、两处信任入口硬化、若干测试确定性/覆盖缺口。

## 实际改动

**P1 语义索引队列卡死**（`semantic.py`、`embedding.py`、`embedding_worker.mjs`、`purge.py`）
- worker 对超过模型 512 token 上限的片段按条目返回 null 向量，只嵌入合法片段；整批推理不再因单个片段失败。
- 新增 `semantic_failures_v1`（space/message/start/状态版本/内容版本/错误码），`refresh` 把逐片段失败记录为显式失败行并从待处理选择中排除；覆盖判定按"向量或当前版本失败行"计算；`status` 新增 `failed_chunks` 报告仍活跃的失败片段。内容/状态版本变化自动重试并清理旧行；purge 同步删除失败行。批量/环境性失败（超时、依赖缺失）仍整体抛错、不写失败行，避免把瞬时故障误标为永久失败。

**P2 修复**
- `semantic.py`：`_semantic`/`_recent` 扫描上限边界先返回已验证命中再检查截止时间——取消/超时恰好落在第 10000/256 行时不再丢弃全部已验证证据。
- `lexical.py`/`runtime.py`：词法索引标识纳入解释器 Unicode 数据库版本（`规则版本×1e6+unidata_version`）；解释器升级后索引如实报 `behind` 并经刷新自愈，不再静默使用与查询不同的归一化表。旧库在下次刷新后自愈，无 schema 迁移。
- `facts.py`：`decide` 的 recorded_at 水位按 subject/predicate 单调钳制，墙钟倒退不会构造从未存在的知识视图。
- `extraction.py`：模型形态合法但超限的候选（`invalid_candidate`）映射为既有公开码 `extraction_invalid_output`（持久类别 `invalid_output`），不再与抽取器崩溃混同为 `extractor_failed`。
- `maintenance.py`：协作式让位（canceled/foreground/time_budget）回队不再写 `error_code`，只有 `processor_failed`/`stale_source` 携带失败类别。
- `purge.py`：重叠预览的目标已被另一确认请求删除时按已完成 no-op 处理，不再永久 `message_not_found`。
- `runtime.py`：`change` 拒绝对 `purged` 阻断态目标改写（`not_recallable`）；`initialize` 三个配置文件改为 temp+fsync+原子替换，消除半写文件卡死目录的窗口（崩溃后仍需手工清理，属文档化限制）。
- `backup.py`：`retain_backups(apply=True)` 单个包被占用/变化/无法删除时报入 `skipped` 并继续，保留报告完整，不中途夭折。
- `archive.py`：`restore_archive` 对任何被占用目标（含非归档文件）统一报 `archive_destination_exists`，与 export 路径一致。
- `mcp_server.py`：启动参数解析改用与 CLI 相同的不回显 Parser，格式错误输出 `evernook:invalid_arguments` 而非回显 argv。
- `cli.py`/`vault_local.py`：缺 tkinter 时 `vault-show` 报 `vault_display_unavailable`；本地显示审计改为尽力而为，审计存储不可用不掩盖真实拒绝码。

**测试与确定性**
- 新增回归：无法向量化片段显式标记且不阻塞队列、内容更新清除失败；recent 扫描上限边界；Unicode 索引标识自愈；墙钟倒退水位钳制；超限草稿错误码分类；协作让位空错误码；租约被接管时原持有者不能发布（导航侧此前未覆盖）；purged 目标拒绝改写；重叠预览 no-op 完成；保留中途卡包跳过；占用目标统一错误码；授权矩阵补 `submit_stream`；MCP 参数不回显；缺 tkinter 诊断；审计失败不掩盖拒绝。
- 两处候选租约测试改为受控时钟（patch `evernook.candidates.time.time`），消除 1.05s/1s 的 ~50ms 墙钟裕量。

## 契约同步

[semantic-v1](../contracts/semantic-v1.md)（逐片段失败与 `failed_chunks`）、[lexical-v1](../contracts/lexical-v1.md)（复合归一化标识）、[facts-v1](../contracts/facts-v1.md)（水位单调与重放语义边界说明）、[changes-v1](../contracts/changes-v1.md)（purged 目标拒绝）、[navigation-v1](../contracts/navigation-v1.md)（协作让位语义）、[encrypted-backup-v1](../contracts/encrypted-backup-v1.md)（保留跳过语义）、[purge-v1](../contracts/purge-v1.md)（重叠 no-op 与冻结范围）、[archive-v1](../contracts/archive-v1.md)（占用目标一致码）、[mcp-v1](../contracts/mcp-v1.md)（启动参数诊断）、[vault-local-v1](../contracts/vault-local-v1.md)（审计尽力而为与 tkinter 诊断）。

## 验证

环境：Windows 固定 NTFS、Python 3.14.4、SQLite 3.50.4、Node 24.12；全部运行数据位于仓库外临时目录，测试只用合成文本/口令。

- 修复前基线：`discover -s tests/contracts` 192 项通过（353.994 秒），`mypy src tests/contracts` 51 文件通过。
- 修复后最终全量：`python -m unittest discover -s tests/contracts -v` **205 项通过，388.851 秒**（192 项既有 + 13 项新增回归）；`mypy src tests/contracts` **51 个文件通过**。
- 插件套件（`npm ci --ignore-scripts` / `typecheck` / `build` / `test`）：**14 项通过**（24.663 秒），合成 provider，无真实模型服务。
- `node --check` 验证 `embedding_worker.mjs` 语法；null 向量语义由合成 backend 回归覆盖（真实 MiniLM 资产不在本机，token 上限的真实触发语料未演示——机制为代码级验证，触发条件取决于 XLM-R tokenizer 对具体内容的分词）。

以上仍是合成环境回归，不是生产验收：未含断网/睡眠/强杀全矩阵、真实桌面安装、长期运行、十万/百万规模或 p95 性能验收。

## 对抗式自审与剩余边界

- 失败行会不会把瞬时故障永久标记？只有 worker 按条目判定为 token 超限才写失败行；批量失败不标记。失败行绑定状态/内容版本，版本变化即失效重试；purge 删除；状态按"仍可召回且版本匹配"过滤，陈旧行不误导。
- 语义覆盖判定放宽（向量或失败行）会不会降低召回安全？不会：扫描仍只 JOIN 向量行；失败行只影响覆盖/待处理统计。
- `change` 的 `not_recallable` 只拦 `purged`，不改 correct/revoked/suppressed 之间的既有改写行为（与 changes-v1 现状一致）。
- `initialize` 原子化缩小了窗口但崩溃后仍可能留下部分文件需手工清理（目录拒绝覆盖属既有测试断言的行为）。
- 本轮未实现：T09 运行时恢复/控制重放、T19 受保护资产删除与可控备份清理、T20 迁移管理、真实宿主接入、断网/掉电/强杀全矩阵、规模与 p95 验收。以上仍以票据为准。
