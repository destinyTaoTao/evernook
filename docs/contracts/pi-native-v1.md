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

## 采集与发布屏障

- 首次和每轮 provider 前保存原生 v3 header、稳定 entry ID、parent ID 与消息；原生消息和审计记录使用独立来源身份，重复重扫幂等。当前分支的真实用户消息作为输入依据，不把注入片段当成新用户事实。fork header 的 `parentSession` 仍保留在审计中；完整 fork/压缩/恢复验收尚未完成。
- pi 的 `context` hook 异常可能被原生扩展机制报告后吞掉。本扩展另外维护阻断状态，并在专用 provider 内再次校验和保存，不能依赖抛异常阻止派发。保存失败不调用目标 provider。
- 一次模型尝试完整缓冲后，正文及 `pi-stream-v1` 附件同事务落盘，再回放 parsed-provider 观察事件及规范化的原生输出事件。附件保留原始 delta 字段、终态和已解析 provider 数据；SDK 的可变 `partial` 缓存不冒充逐帧原件，也不声称取得 HTTP/SSE 原始字节。
- 这会推迟显示到整个尝试完成。默认上限 768 KiB，可配置但不超过 1 MiB；超限、无终态、失败、取消或敏感信号均阻断未发布内容。信号同时传给委托 provider；回放观察事件期间取消也停止余下发布。提交后、显示前崩溃可能留下已存但未显示的记录，不保证恰好显示一次。
- 工具仅限 `tools` 中明确包装的定义。调用参数先保存，再执行真实工具；部分更新被缓冲，完整结果及 `pi-tool-v1` 附件保存后才通知原生 UI。合并检查跨更新拆分的敏感信号。工具失败保留明确缺口标记，不将原始异常、未保存前缀显示出来。工具已发生的外部副作用不能回滚，取消也依赖工具遵守 signal。

当前 profile 是文本专用：原生 image 输入/历史和 image 工具结果被阻断，不能把 base64 当普通 JSON 保存。未实现通用文件/多模态附件采集。其他插件自己写的日志、工具内部自行发布的内容及 native session 预先写入的明文不受本扩展控制。有限规则不能识别所有秘密；本 profile 未自动接入保险库，检测到的原件拒收并阻断。

## 注入、重扫与关闭

普通召回返回来源后，重新读当前版本，展开至多 2048 code point，保留坐标、版本和缺口；总片段字符预算默认 12000。证据作为临时 `role=user` 的不可信历史数据进入当前上下文，不修改系统人设，不授予工具权限。此字符上限不等于 T16 的模型 tokenizer 预算；完整事实/置顶/context-builder 仍待原生接线。

`/evernook-rescan`、返回对象的 `rescan(ctx)` 可重扫当前原生会话；`capture(ctx)` 依来源去重。`session_shutdown` 取消本扩展的在途请求并关闭连接，新的会话可惰性重连；返回对象的 `close()` 释放 hooks、连接和 provider。未验证全部第三方扩展组合或无限历史扫描性能。

验收使用真实 pi SDK、dsh 内核、SQLite 和 stdio MCP，模型为确定性合成 provider。跨入口共享已经过内核级验证；真实模型、日常 CLI 加载、系统断网、长时间运行、进程强杀和人工 UI 验收仍须独立完成。
