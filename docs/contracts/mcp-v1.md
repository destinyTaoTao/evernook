# T11：统一记忆 MCP v1

对应 [Issue #12](https://github.com/destinyTaoTao/evernook/issues/12)。T11 仅依赖已实现的 T07，不依赖仍待备份验收的 T09/T10。本次提供本机 stdio 适配，不注册日常宿主、不监听网络，也不绕过 [Python 核心契约](python-text-v1.md)。

## 安装与可信配对

使用 Python 3.14 的仓库外虚拟环境，从 checkout 执行 `python -m pip install -e ".[mcp]"`；开发/完整回归使用 `.[dev]`，包含 mypy 和同一 SDK。适配采用官方 [MCP Python SDK v2.3.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)，显式固定版本；核心运行时仍无第三方依赖。实际回归使用 SDK 的 stdio 子进程，而非内存调用。

可信的本地操作端先初始化普通核心目录，再显式配对客户端；以下仅为合成配置示例，`E:\EverNookData` 尚未被本次任务初始化：

```python
from pathlib import Path
from evernook import initialize, pair_client

# 只在新的专用目录执行一次；已有目录必须直接使用其 runtime.json。
config, owner = initialize(Path(r"E:\EverNookData"), client_id="synthetic-owner", entry="synthetic-native")
access = pair_client(config, client_id="synthetic-mcp", entry="synthetic-mcp",
                     access_path=config.parent / "mcp-access-synthetic.json")
```

`pair_client` 是可信本机维护操作，不是 MCP 工具。新身份只获得共享普通记忆权限，默认无归档审查权限；不增加保险库能力。client ID 与入口一一绑定，不能占用另一个身份的入口。同一 client/entry/path 重试返回原身份；冲突或访问文件内容不同拒绝覆盖。注册通过数据库短写锁串行化，凭证文件在私有目录内 fsync 后原子替换；注册成功但访问文件写入失败时，重试可补齐文件而不轮换令牌。未声称此维护操作经过物理掉电验收。

身份登记文件升级为严格 v3 `clients` 清单；旧 v1/v2 自动按单身份读取，不自动增加权限。所有普通操作每次读取最新登记并校验调用端身份、token、契约版本；已连接进程不会无条件继续使用过期登记。SQLite 仍为同一个权威账本，没有另建 MCP 数据库。

模型查询启动命令只包含路径及可选的本地模式：

```text
<venv-python.exe> -m evernook.mcp_server --config E:\EverNookData\runtime.json --access E:\EverNookData\mcp-access-synthetic.json
```

令牌只从受 ACL 保护的访问文件载入，不在参数、环境变量、工具参数或协议客户端名称中传递。该文件必须位于同一运行目录，启动前检查文件及目录 ACL、严格字段/类型、重复 JSON 键和实际核心认证。`clientInfo.name` 只是协议元数据，不授予身份。路径只是宿主的可信启动配置，模型工具不能修改路径或重新绑定身份。同一 Windows 账户内进程能够读取账户文件的边界同核心，不能把它称为同账户沙箱。

## 默认查询模式与可信宿主迁移

[Issue #30](https://github.com/destinyTaoTao/evernook/issues/30) 将默认启动行为收敛为 `--profile recall`：仅开放 `search/recall/read/read_source/read_important/state/status/facts` 八个查询工具（含T15召回与T13事实查询）。`remember/mark_important/change/list/capture/capture_stream` 不出现在工具列表中，直接点名调用也返回 `operation_not_allowed`，不会执行核心操作。工具参数不能修改 profile。状态的 `adapter.profile/writes_allowed` 描述适配器权限；`capabilities` 仍描述核心已实现的能力，不表示当前模型拥有全部能力。

**兼容性变更：已有写入/列表调用端必须由可信本地维护者在启动参数中显式加 `--profile trusted-host`**，才继续开放下表工具。Python 核心契约与数据格式不变。该模式只供已核实角色、源事件及用户管理意图的可信宿主使用；不得仅为消除模型工具报错而给普通模型启用。初始化和配对也可以使用[本地 CLI](cli-v1.md)。T10 另加可信 `capture/capture_stream` 两项入口，见 [dsh 契约](dsh-native-v1.md)：附件通过严格 base64 编码传输，流在检查后复用 capture 原子事务；默认查询模式继续拒绝这两项操作。

认证只证明调用端持有访问文件，不能证明 `role=user` 来自真实用户，也不能证明 `evidence_id` 是用户授权。在完整模式下，核心能校验来源绑定、证据存在与版本，却不能识别模型自行编造的用户角色事件；原生宿主必须在模型之外生成来源，并把更正/抑制等用户管理意图接到可信交互。profile 是受信任启动配置的限制，不是持久化 token scope，也不防御已取得 shell/访问文件的同账户进程。

`recall` 仍可按查询与指定 ID 读取共享普通记忆，不提供按项目/用途/客户端的细粒度内容隔离，也不保证通过多次查询无法遍历数据。它减少默认工具权限，没有改变已确认的共享语义。被取回的文本仍为不可信数据，不可提升成系统指令。原生自动采集/每轮注入仍待真实宿主验收，不能从 MCP 可连接推导为已经支持。

## 工具与结果

所有工具使用前缀 `evernook_v1_`，输入 JSON Schema 由严格类型生成，包括嵌套对象的 `additionalProperties=false`，不自动转换 bool 为整数等错误类型。下表及T10采集工具用于 `trusted-host`，默认 `recall` 仅有上述八项。

| 工具后缀 | 输入 | 成功结果 |
|---|---|---|
| `remember` | `event`，同核心 Event/Source | 稳定持久化 Receipt；敏感原件未保存时明确 `projection_only` |
| `read` / `read_source` | `message_id` / `source` | 当前允许普通读取的原文或安全投影及回执 |
| `list` | 可选 `since/until/limit/offset` | 时间范围 `[since,until)` 的 Page，保留核心分页语义 |
| `search` | `query`，可选 `mode/since/until/limit/offset` | [词法/精确证据](lexical-v1.md)的来源、版本、原文坐标及索引覆盖/降级信息 |
| `recall` | `query`；可选 `limit/as_of/recent_days/recent_target_chars/min_similarity/timeout` | [语义融合与自适应近期证据](semantic-v1.md)、实际覆盖范围及降级原因；不返回已接纳事实 |
| `facts` | `subject_id/predicate`，可选 `valid_at/known_at` | [显式时态事实](facts-v1.md)、计划/争议、来源版本及不可用状态；不提供事实接纳 |
| `mark_important` | `mark`，同 ImportantMark | 已可靠提交的重要引用 |
| `read_important` | `important_id`，可选 `cursor/limit/content_version` | 原件/投影 code point 分页，不返回摘要 |
| `change` | `change`，同 Change | `correct/revoke/suppress` 的版本化回执；不提供物理删除 |
| `state` | `message_id` | 当前状态/版本及更正引用，无归档原文 |
| `status` | 空对象 | 能力、已提交计数、任务待处理数量；`processing.lexical` 返回真实索引覆盖/水位，`index_ready` 仅表示该索引当前追平；消费者未运行 |

成功均为 `{"contract_version":1,"ok":true,"result":...}`；失败 `isError=true`，正文与 structuredContent 均为固定形状，例如 `{"contract_version":1,"ok":false,"error":{"code":"version_conflict"}}`。实际 SDK v2 的 Python 属性是 `is_error/structured_content`，线上 JSON 使用 MCP 定义的 `isError/structuredContent`。

`message_not_found`、`not_recallable`、`unauthorized_source`、`source_conflict`、`version_conflict`、`storage_busy/storage_unavailable` 等复用核心失败语义；格式不符为 `invalid_arguments`，未知工具为 `unknown_tool`。不暴露异常栈、入参或凭证。启动失败只向 stderr 写固定错误码；stdio stdout 专用于协议。SDK 请求日志及默认遥测中间件关闭，不把对话发送到诊断管道。

stdin 显式使用严格 UTF-8 reader，覆盖 SDK 默认的替换解码；非法线上字节会中止连接、报告固定 `invalid_encoding`，不会以 U+FFFD 替换后生成原件成功回执。调用端恢复连接后需按原来源重试合法输入。普通工具参数错误保持会话可继续调用。

合成调用示例：

```json
{"name":"evernook_v1_read","arguments":{"message_id":"synthetic-known-id"}}
```

```json
{"name":"evernook_v1_change","arguments":{"change":{"request_id":"synthetic-change-1","target_id":"synthetic-old-id","expected_version":1,"action":"suppress","evidence_id":"synthetic-user-instruction-id","reason":"synthetic suppression"}}}
```

有效 ID 必须来自先前保存，示例不是预置数据。修改需要完整用户来源证据，重试幂等、旧版本冲突；重要片段继续核对表示、版本、原文和 code point 坐标。成功读取只向已绑定客户端交付其请求的普通内容；历史记录始终是不可信数据，不提升指令优先级。没有归档审查、配对、配置修改、解密、外部动作或权限提升工具；`sensitive=false` 不能关闭强制检测。

## 已验证与未启用

使用真实 SDK 客户端验证现代协议与 legacy initialize、两个已配对客户端共享、Unicode 原文/核心共用断言、重要标记与抑制、重启重试、错误版本/令牌、来源冒用、敏感投影、严格参数、真实 SQLite 忙锁及插入失败。诊断输出用合成哨兵检查无请求原文或令牌。

T14 新增真实 stdio `search` 回归：索引未建与维护后返回相同可校验原文，状态反映覆盖与降级，敏感查询拒绝。T15 后完整模式 11 个工具、默认查询模式 7 个。索引刷新/重建仅供可信本机维护端使用，不授予模型维护权限。待处理任务数不能理解为已建立检索索引；消费者未运行，词法索引追平不代表模型或整体处理完成。网络 HTTP/SSE、真实 dsh 展示屏障、MCP 导出/恢复、备份及正式模型质量仍未启用或验收；保险库保持关闭。备份目的地、启用和恢复验收按用户决定待办。

T15 可在可信启动配置中加 `--embedding-config <absolute-path>` 显式接入固定本地 MiniLM 候选；未配置时新 `recall` 工具仍返回词法证据和 `semantic_disabled`。配置不能从工具参数修改，向量维护仍只供可信本地端使用。`status.semantic` 报告配置空间覆盖；详情与实际边界见[语义契约](semantic-v1.md)。
