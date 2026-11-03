# T25 pi 原生适配增量

对应 [#26](https://github.com/destinyTaoTao/evernook/issues/26)。固定 `@earendil-works/pi-coding-agent` / `pi-ai` `1.4.0`、Node `24.11.x`，复用 `plugins/harnesses` 的可信 MCP 连接与同一 Python 账本。此增量是显式选择的 SDK 接线能力，未修改日常 pi 配置或宣称桌面安装验收。

## 专用 SDK profile

安装与构建命令见 [插件 README](../../plugins/harnesses/README.md)。Python 环境需要 `.[mcp]`，可信本地操作端预先初始化运行目录、配对 `entry=pi` 并检查 ACL。使用真实宿主的 `ModelRuntime` 和已配置的目标模型：

```ts
import { installPiMemory } from '/absolute/path/to/evernook/plugins/harnesses/dist/pi.js';

// 放在 DefaultResourceLoader 的 extensionFactories 中。
async function memoryExtension(pi) {
  const memory = await installPiMemory(pi, {
    python: configuredPython,
    config: configuredRuntimeFile,
    access: configuredPiAccessFile,
    cwd: configuredCheckout,
    entry: 'pi',
    target: actualConfiguredModel,
    tools: compatibleToolDefinitions,
  });
  // createAgentSession 的 model 选择 memory.model。
}
```

上面变量由宿主提供，不是可直接执行的独立启动器。包装 provider 默认为 `evernook-memory`，委托 `ctx.modelRegistry.streamSimple(target, ...)` 解析目标模型原有认证，不复制 token，不发明端点。wrapper 自身不需要 API key；可信 MCP 访问文件不交给聊天模型。

`createAgentSession` 必须用 `noTools: 'builtin'` 屏蔽未包装的内置工具，保留本扩展登记的明确工具集合；纯聊天可用 `noTools: 'all'`，但这也会屏蔽自定义工具。未包装工具被拒绝。该 SDK profile 不是任意现有扩展/CLI profile 的透明全局拦截器。

可显式设置 `maintenance: { access, onReview, onResult }` 接入与 dsh 共用的[确认式维护](host-maintenance-v1.md)。维护身份必须独立，普通连接仍只含 capture/recall；模型只有经过工具保存屏障的提议入口，后续当前分支真实用户确认才在最终 provider 边界执行。可信 UI 使用返回的 `maintenance` 展示/拒绝预览和读取分页；重启失去待确认预览，已提交核心状态继续有效。启用此工具时使用 `noTools: 'builtin'`。

## 采集与发布屏障

- 首次和每轮 provider 前保存原生 v3 header、稳定 entry ID、parent ID 与消息；原生消息和审计记录使用独立来源身份，重复重扫幂等。当前分支的真实用户消息作为输入依据，不把注入片段当成新用户事实。fork header 的 `parentSession` 仍保留在审计中；完整 fork/压缩/恢复验收尚未完成。
- pi 的 `context` hook 异常可能被原生扩展机制报告后吞掉。本扩展另外维护阻断状态，并在专用 provider 内再次校验和保存，不能依赖抛异常阻止派发。保存失败不调用目标 provider。
- 默认 buffered 兼容/验证模式在一次模型尝试完整缓冲后，正文及 `pi-stream-v1` 附件同事务落盘，再回放 parsed-provider 观察事件及规范化的原生输出事件。附件保留原始 delta 字段、终态和已解析 provider 数据；SDK 的可变 `partial` 缓存不冒充逐帧原件，也不声称取得 HTTP/SSE 原始字节。显式可靠文本模式见下文。
- 这会推迟显示到整个尝试完成。默认上限 768 KiB，可配置但不超过 1 MiB；超限、无终态、失败、取消或敏感信号均阻断未发布内容。信号同时传给委托 provider；回放观察事件期间取消也停止余下发布。提交后、显示前崩溃可能留下已存但未显示的记录，不保证恰好显示一次。
- 工具仅限 `tools` 中明确包装的定义。调用参数先保存，再执行真实工具；部分更新被缓冲，完整结果及 `pi-tool-v1` 附件保存后才通知原生 UI。合并检查跨更新拆分的敏感信号。工具失败保留明确缺口标记，不将原始异常、未保存前缀显示出来。工具已发生的外部副作用不能回滚，取消也依赖工具遵守 signal。

当前 profile 是文本专用：原生 image 输入/历史和 image 工具结果被阻断，不能把 base64 当普通 JSON 保存。未实现通用文件/多模态附件采集。其他插件自己写的日志、工具内部自行发布的内容及 native session 预先写入的明文不受本扩展控制。有限规则不能识别所有秘密；本 profile 未自动接入保险库，检测到的原件拒收并阻断。

## 注入、重扫与关闭

兼容模式普通召回返回来源后，重新读当前版本，展开至多 2048 code point，保留坐标、版本和缺口；总片段字符预算默认 12000。证据作为临时 `role=user` 的不可信历史数据进入当前上下文，不修改系统人设，不授予工具权限。显式设置 `contextConfig` 与 `memoryBudget` 可启用[同一核心的目标 tokenizer 预算模式](host-token-budget-v1.md)，组合完整事实/置顶/人设/画像并核对最终目标请求；字符兼容模式仍单独标识，不能冒充 token 预算。

`/evernook-rescan`、返回对象的 `rescan(ctx)` 可重扫当前原生会话；`capture(ctx)` 依来源去重。`session_shutdown` 取消本扩展的在途请求并关闭连接，新的会话可惰性重连；返回对象的 `close()` 释放 hooks、连接和 provider。未验证全部第三方扩展组合或无限历史扫描性能。

验收使用真实 pi SDK、dsh 内核、SQLite 和 stdio MCP，模型为确定性合成 provider。跨入口共享已经过内核级验证；真实模型、日常 CLI 加载、系统断网、长时间运行、进程强杀和人工 UI 验收仍须独立完成。

## 可靠文本模式（M58-15）

[#73](https://github.com/destinyTaoTao/evernook/issues/73) 允许显式 `streamMode: 'durable_text'`，返回模型 api 为 `evernook-durable-pi-v1`。同一模式不能配置 tools 或 maintenance；与 dsh 一样，只支持 index=0 的单文本块，工具、thinking、多块和不一致终态阻断。原生工具集仍须屏蔽。默认 buffered 不自动升级为日常可靠流式 profile。

依据固定 1.4.0 的 `pi-agent-core/dist/agent-loop.js:286–312`，原生循环从 provider 事件流读取后才发送 message_update；包装端在 output.push 之前等待与 dsh 共用的 durableText/capture_chunk 屏障。[固定版本自定义 provider 文档](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/custom-provider.md)描述 SDK 事件协议，本票以锁定包源码和真实 SDK 回归为运行证据。

委托 provider 延迟到核心流初始化成功后启动。pi 事件转换只取文本 delta、text_end 和校验后的最终文本/原因，丢弃原始 partial、签名、响应 ID、usage/cost 和其他 provider 元数据；onProviderStreamEvent 不透传。发送给 SDK 的 partial 从已提交允许范围重新构建，不能因 SDK 的共享可变缓存提前暴露尾部。此模式的 usage/cost 是 SDK 必需的零值占位，不代表已测量的真实消耗，不能用于计费或性能结论。stop 完成；length 保留 failed/未完成范围；取消和 provider 错误保留已发布安全前缀，错误回执只含固定诊断。原生 assistant settlement 不再另行导入普通答案，防止截断/失败片段被标成完整；启用前 native 助手历史仍留在原生会话，不由此模式自动导入完整原文。

| 路径 | durable_text | buffered 兼容/验证模式 |
|---|---|---|
| 单文本块 | 核心逐块提交后发布；尾部可能延迟 | 整次提交后发布 |
| 明确包装 tools/maintenance | 配置拒绝；模型工具帧阻断 | 保留调用前保存、更新缓冲、结果保存后发布 |
| 未包装/内置工具 | 不支持，必须屏蔽 | 仍拒绝 |
| thinking/多块/图像 | 阻断 | 既有范围不变，图像仍阻断 |
| provider 原始观察事件/元数据 | 不转发、不声称已归档 | 通过完整采集后回放 |

`streams(sessionId, offset=0)` 和 `readStream(source, cursor=0, limit=2048)` 与 dsh 共享账本搜索和核心分页协议。分页可因其他会话出现空页，应跟随 next_offset。reload 后从新 MCP 进程回取已提交部分，重复 `/evernook-rescan` 不重建丢失尾部、不伪造完整回答。headless SDK reload 后旧 ctx 会失效，应通过新命令回调的 ctx 扫描；不能缓存旧 ctx。renderer 自行维护显示游标；没有自动续写旧流或恰好显示一次保证。提交结果不确定时仍保留 open 供回查。

`status(sessionId).stream_timing` 的 `first_delta_to_publication_ms` 是包装端取到第一个非空 provider text_delta，到首个允许文本事件入队的单调时钟耗时，包含提交和安全尾部等待；`request_to_publication_ms` 从包装 provider 被调用起算，还包含其输入复核和上下文构造，未包括更早的原生 context hook。无可见文本时没有该值，每次调用重置。不是 provider 内部生成时间、HTTP/SSE 时延或桌面绘制时间。合成测量及验证边界见[实施报告](../reports/m58-15-pi-durable-stream.md)。
