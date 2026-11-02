# M58-09/10：dsh/pi 本地目标 tokenizer 与完整上下文预算

`contextConfig` 与 `memoryBudget` 同时启用 dsh 的目标 tokenizer 模式。Python MCP 的 `--context-config` 指向仓库外可信设置文件；每次调用重新读取设置，复用 `ContextBuilder` 组合置顶、人设、画像、指定事实及经验和相关证据。查询超过 256 code point 时保留完整原始输入，省略查询召回并在 payload 中披露 `query_too_long`，不会截取前缀冒充完整查询。

设置文件示例（路径与散列均需替换）：

```json
{"schema_version":1,"tokenizer_config":"E:/configured-memory/counter.json","policy":{"normal_tokens":16000,"deep_tokens":64000,"pinned_tokens":4000,"recent_days":7,"recent_target_chars":8192,"persona_tokens":600,"profile_tokens":400,"pinned_ids":[],"fact_keys":[],"experience_ids":[]}}
```

`counter.json` 必须包含 `schema_version: 1`、绝对路径 `node`、`package_dir`、`tokenizer_file`、`config_file`，以及 `model_id`、`tokenizer_sha256`、`config_sha256`。`package_dir` 是可信安装的 `@huggingface/tokenizers` **0.2.0** 目录；两份模型文件分别为目标模型的 `tokenizer.json` 与 `tokenizer_config.json`，以真实 SHA-256 固定。模型和运行设置放在 checkout 外，启动不下载资源。分词库需支持所选 tokenizer；不支持、资产变化或计数失败时明确失败，没有字符估算回退。计数身份绑定模型、文件散列和库版本。

dsh `memoryBudget` 由可信宿主提供 `provider`、`modelId`、`memoryTokens`、`contextWindow`、`outputReserveTokens` 和同步 `countRequest(request: GenerateOptions): number`。该回调必须按对应 provider 的实际聊天模板、工具声明与请求封装使用真实目标 tokenizer 计数；它不是 SDK 自带的通用计数器。没有可靠的 provider framing counter 时不能启用此模式。输出额度受预留额度限制；已知模型窗口与配置窗口取较小值。宿主历史或模板占满窗口时，在调用 provider 前报 `host_context_window_exceeded`，本票不自动压缩宿主历史。

核心计数覆盖完整序列化 payload。宿主使用同一固定资产独立复算，并计入 SDK runtime-context 包装；最终请求仅保留与本轮内容匹配的一份 EverNook section，其余宿主 section 保留。旧记忆快照不会继续挤占额度或携带失效画像。文件先转换为 SDK 的实际 handle 文本，工具使用完整当前声明，避免后续历史工具更新投影改变计数。文本模型图片占位可按 SDK 投影计数；未知或图片模型的图片请求返回 `budgeted_image_request_unsupported`，尚不声明多模态预算支持。输出仍经过既有完整保存屏障；可靠分块流式另见后续票据。

`installMemory(...).settings()` 区分已有伴侣预设的推荐值、当前文件中的保存值及宿主限额；推荐值不自动覆盖保存值，也不是性能承诺。每会话 `.status()` 报告实际 token 上限、核心计数、SDK 包装计数、省略和降级。普通配对身份只需 `capture/recall`，不持有 `accept/admin`。MCP `context` 参数不能修改可信设置或传入 tokenizer 路径。

未提供这两项配置时保留兼容字符模式，状态明确标为 `legacy_character_cap`；该模式不等于真实 token 预算验收。真实 SDK 合成测试使用真正的本地 WordLevel tokenizer 与合成 provider，验证真实 transport、保存、组合注入、独立计数和失败边界；它不证明某个已部署模型的 tokenizer、provider 模板或桌面插件加载已验收。每次计数启动有界本地 Node 子进程，尚未校准长期运行和冷启动性能。

## pi 复用与取消

pi 同样通过 `contextConfig` 和 `memoryBudget` 显式启用。保存策略、目标 tokenizer、事实与可见性检查完全复用同一核心；它在最终 provider 包装入口构造临时用户数据消息，不将注入片段写回原生用户历史。`countRequest` 接收 `{model, context, options}`，必须按真实目标 provider 的封装计数；输出额度与窗口还受物理目标 `Model` 的限制。`pi-virtual` 会在计数后切换物理路由，因此此模式明确拒绝。纯文本限制在最终 transcript 再次检查，其他扩展后加的图片也拒绝。

pi `.settings()` 暴露推荐和保存策略，`.status(sessionId)` 暴露上次构造元数据及当前错误码。窗口不足、非法配置、取消等不会被伪装成成功或空召回。两个入口均可用独立 `capture/recall` 身份共享最新普通证据，保留来源/分支与幂等语义。

宿主 AbortSignal 传到 MCP context 请求；取消通知设置核心协作事件，停止后续构造并回收正在运行的计数子进程。SQLite、ACL 与配置读取仍有自身边界，这不是硬实时取消或端到端时延保证。pi 取消后不调用目标 provider，已有原始输入保持已提交状态。
