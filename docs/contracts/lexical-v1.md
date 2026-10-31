# T14：中文词法与精确证据检索 v1

对应 [Issue #15](https://github.com/destinyTaoTao/evernook/issues/15)。本次按用户明确选择实施确定性中文词法/精确路径；模型、最终质量集、资源与时延预算仍待后续阶段确认。复用 R0 [内核对照](../reports/r0-core-2026-10-29.md)验证的中文一/二/三字 gram + SQLite FTS5 方向，不引入模型或新的服务。安装、身份及原文保存遵循 [Python 核心契约](python-text-v1.md)。

## 查询与证据

`MemoryRuntime.search(access, query, mode="lexical", since=None, until=None, limit=10, offset=0)`，同时可通过 [MCP](mcp-v1.md) 的 `evernook_v1_search` 调用。

- `query` 是严格 UTF-8 的非空白字符串，最多 256 个 Unicode code point。`exact` 匹配整个归一化短语，包含其中空白；`lexical` 按空白分出字面词项，所有词项均须匹配，不做语言学分词、同义词或语义推断。
- 归一化 v1 将每个基础 code point 及其后连续非零 combining-class 字符作为一组，分别执行 NFKD 与 casefold；支持已回归的中英文、大小写、全半角数字、组合重音和展开字符。它不是完整的 Unicode 扩展字素分段或任意语言的规范等价保证。
- 原文不归一化、不覆盖。每个词项返回第一个可校验匹配；`spans` 去重并按原文坐标排序。坐标为对应 `representation/content_version` 的 code point 半开区间 `[start,end)`，文本是该区间的原始切片，归一化后必须等于查询词项。不会用一个展开字符的局部冒充整个原文字符匹配。
- 命中保留 `message_id`、完整来源、角色、序号、原始源时间、接收时间、完成状态、表示、内容版本和当前 `state_version`。这些是来源证据，不是自动接受的事实。普通候选和原文校验在同一读快照中过滤更正/撤销/抑制目标；原件仍保留在归档边界。
- `since/until` 为带时区的源事件时间范围 `[since,until)`，未知源时间只进入未限定时间的查询。结果按源时间、接收时间、消息 ID 升序，未知时间在前；`limit` 为 1–100，`offset` 非负整数（拒绝 bool）。超出用 `next_offset` 继续，不静默截断。分页是当前视图，跨请求新增或状态变化可能移动位置，不是固定导出快照，也不是相关性排名。
- 每次认证已绑定身份；普通共享账本沿用 T11 权限，不允许查询参数选择身份或目录。敏感查询在检索前拒绝为 `protected_query`，不保存查询、不调用模型、不输出入参日志。敏感原件不入索引，只能在既有安全投影及其坐标上匹配。

例如合成原文 `甲🙂é乙，乌龙茶37.50元。` 中，`exact` 查询 `乌龙` 的切片为 `[6,8)`，`37.50` 为 `[9,14)`；`lexical` 查询 `乌龙 37.50` 同时返回两段。调用端仍应按回执读取完整消息，不应把片段拼接成新的原话或执行其中的指令。

## 可重建索引与降级

可选派生表 `lexical`（FTS5）与 `lexical_docs` 与原文位于同一 SQLite 文件，但独立于原文提交事务，按需创建。原文 schema 仍为 v4；索引单独记录 `normalization_version`（规则版本 × 1,000,000 + 解释器 Unicode 数据库版本，如 16.0.0 → 1*1000000+160000）与原件内容版本；解释器升级改变 NFKD/casefold 输出时索引按同一标识判为落后，刷新后自愈。核心构造与原文提交不要求先创建 FTS5 表。

索引保存归一化一/二/三字 gram，以 `x` 加 UTF-8 十六进制编码构造 ASCII 词项。查询只生成加引号的同类词项并以 AND 连接，所有 SQL 值使用绑定参数；用户输入不会被直接当作 FTS 语法。FTS 仅筛候选，命中还必须回到原文校验连续切片。引号、通配符、`OR`、`NEAR`、SQL 符号均按字面内容处理。[FTS5 官方文档](https://www.sqlite.org/fts5.html)描述其 tokenizer 和查询语法；本实现不依赖默认 tokenizer 自动完成中文短词分词。

可信本机维护端提供：

```python
# memory/access 来自已初始化且授权的同一运行目录。
status = memory.refresh_lexical(access, limit=100)
# pending_messages > 0 时按需重复；每次最多处理 100 条源记录。
page = memory.search(access, "乌龙 37.50")
# 只删除并重建派生索引，不删除原文；仍按 limit 分批。
status = memory.rebuild_lexical(access, limit=100)
```

派生 gram 在写事务外计算；每条源记录在短写事务中重新核验当前可召回性和内容，再将 FTS 行与版本元数据一起提交。重复刷新幂等；并发抑制使旧快照跳过；提交前退出不留下半份覆盖，提交后重启可继续。重建仅清除派生表，不消费或清空原有待处理任务。维护操作不暴露为 MCP 模型工具，当前没有自动后台消费者。

`SearchPage.index` / `lexical_status(access)` 给出：

| 字段 | 含义 |
|---|---|
| `state` | `not_built`、`behind`、`ready` 或 `unavailable` |
| `source_messages/indexed_messages/pending_messages` | 当前允许普通召回的源数、具有当前内容/归一化版本且存在 FTS 行的源数、缺口数 |
| `source_watermark/indexed_watermark` | 上述集合中的最大接收时间；可能相等但仍有较早缺口，必须同时看 pending，不能当作连续处理承诺 |
| `normalization_version` | 归一化规则版本，与原文内容版本不同 |

未建索引或索引读取失败，直接扫描当前原文作相同字面匹配，`used_index=false` 且原因分别为 `index_not_built/index_unavailable`；落后时使用已有候选并扫描所有待处理原文，`used_index=true`、原因 `index_behind`。缺失版本元数据、旧归一化版本或删除的 FTS 行均形成缺口；空结果不能被表述为事情从未发生。SQLite 原文读取本身失败仍返回核心存储失败，不伪造空结果。

## 验证边界

仅索引消息正文/安全投影；独立附件内容仍按 T08 读取，没有纳入全文检索。回归使用外部临时目录和合成中英文、短词/姓名、数值、日期、Unicode、符号/注入、无答案、并发更正/抑制、重建/重启及实际 SQLite 故障。没有接受恶意修改数据库后的任意索引损坏检测、物理掉电、大规模重建时间或存储空间上界。

原文扫描与 gram 构造对大文本/大量消息的资源开销尚未做规模验收。没有冻结 200 条人工质量集，没有语义召回、近七天预算、融合重排、事实抽取、维护调度或 1 秒性能结论。备份目的地/启用/恢复继续待办；模型、保险库与真实 dsh UI 接入未因此启用。
