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

启动命令只包含路径：

```text
<venv-python.exe> -m evernook.mcp_server --config E:\EverNookData\runtime.json --access E:\EverNookData\mcp-access-synthetic.json
```

令牌只从受 ACL 保护的访问文件载入，不在参数、环境变量、工具参数或协议客户端名称中传递。该文件必须位于同一运行目录，启动前检查文件及目录 ACL、严格字段/类型、重复 JSON 键和实际核心认证。`clientInfo.name` 只是协议元数据，不授予身份。路径只是宿主的可信启动配置，模型工具不能修改路径或重新绑定身份。同一 Windows 账户内进程能够读取账户文件的边界同核心，不能把它称为同账户沙箱。

## 工具与结果

所有工具使用前缀 `evernook_v1_`，输入 JSON Schema 由严格类型生成，包括嵌套对象的 `additionalProperties=false`，不自动转换 bool 为整数等错误类型。

| 工具后缀 | 输入 | 成功结果 |
|---|---|---|
| `remember` | `event`，同核心 Event/Source | 稳定持久化 Receipt；敏感原件未保存时明确 `projection_only` |
| `read` / `read_source` | `message_id` / `source` | 当前允许普通读取的原文或安全投影及回执 |
| `list` | 可选 `since/until/limit/offset` | 时间范围 `[since,until)` 的 Page，保留核心分页语义 |
| `mark_important` | `mark`，同 ImportantMark | 已可靠提交的重要引用 |
| `read_important` | `important_id`，可选 `cursor/limit/content_version` | 原件/投影 code point 分页，不返回摘要 |
| `change` | `change`，同 Change | `correct/revoke/suppress` 的版本化回执；不提供物理删除 |
| `state` | `message_id` | 当前状态/版本及更正引用，无归档原文 |
| `status` | 空对象 | 能力、已提交计数、任务待处理数量；真实声明消费者和索引尚未运行 |

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

MCP 不提供新的后台消费者、水位或索引完成承诺；状态明确不可用，不能把待处理任务数理解为已建立检索索引。网络 HTTP/SSE、真实 dsh 展示屏障、导出/恢复、备份和模型仍未启用或验收；保险库保持关闭。备份目的地、启用和恢复验收按用户决定待办。
