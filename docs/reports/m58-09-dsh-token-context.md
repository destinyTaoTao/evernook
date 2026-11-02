# M58-09：dsh 目标 token 预算接线

Issue #67 以显式可信配置连接既有 `ContextBuilder` 与真实本地 tokenizer。配置绑定目标模型、资产散列及 tokenizers.js 0.2.0。MCP 只暴露有保存策略上限的只读构造操作，不接收配置路径或管理权限。dsh 宿主独立复算完整 payload 与 SDK 包装，并在最终 SDK 文件/工具投影后检查宿主输入、模板、历史与预留输出额度。保存设置逐轮重读；推荐、保存与实际额度分别可查询。

真实 SDK 回归通过普通 `capture/recall` 配对身份驱动 stdio MCP 与 SQLite；合成 provider 收到低信任的人设、画像、事实、重要交代与证据。长查询披露省略，不取查询前缀。旧 EverNook runtime-context section 在最终请求中剔除，保留其他宿主 section；空 payload 不保留旧记忆。输出继续经过既有完整保存后发布屏障。

两路审查复现并修复了多轮快照合计超限（750 > 512）、SDK 后续文件/工具投影改变计数，以及非法事实键字符串被拆成两字符键的问题。修订后独立两轮复测仅一份记忆、373 ≤ 512；文件/工具回归核对预算计数与 provider 实际请求一致。

验证：原生 dsh/pi 整套 21 项通过；Python tokenizer 3 项、上下文 15 项、MCP 15 项、权限 6 项、人设 6 项通过；源码及相关测试严格类型检查、TypeScript 检查和构建通过。修复了权限/人设测试中的两项既有类型错误。全量 Python 测试类型检查仍有 `test_consolidation_tiers.py` 中 3 条既有未使用 ignore，留待相应沉淀增量处理。

测试使用真正的本地 WordLevel tokenizer 和合成 provider，不冒充已部署模型模板验收。宿主 framing counter 必须与目标 provider 的实际序列化一致；图片未知或图片模型请求明确拒绝，工具使用完整当前声明。旧字符模式仍可兼容但明确标识。没有启用真实桌面日常 profile、pi 预算模式、可靠分块流式、自动压缩或未经测量的性能承诺。
