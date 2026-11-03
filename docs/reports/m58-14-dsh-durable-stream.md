# M58-14：dsh 可靠文本发布与中断回放

Issue #72 在固定 dsh 0.2.1-rc.3 的真实 llm/stream → live.push 发布边界接入核心 capture_chunk。显式 durable_text 模式逐块等待提交回执，只发布允许范围；默认 buffered 保持兼容/验证身份。单文本块、Unicode 代理对和大 delta 的字节分块已覆盖；工具、reasoning、多块和不一致最终块会阻断。原始元数据不转发，也不把原生未完成 settlement 二次导入成完整答案。

使用用户确认的两个验收边界：真实 SDK 发布事件，以及新 MCP 客户端的持久回放。首条红测试复现 provider 尚未完成时旧缓冲屏障没有发布前缀，接线后通过。第二条红测试发现 max-tokens 会错误生成完整回答，已改为 failed/未完成，不释放尾部、不生成完整普通回答。

定向回归覆盖：取消后新适配器重扫两次、真实 SDK 子进程在发布后强杀（没有 native settlement 仍可发现回放）、中途 SQLite 写入失败阻断新片段并保留 open 供回查、分块敏感输出与工具执行阻断、tokenizer 预算模式下的大 Unicode delta、provider 错误/不一致最终块的诊断及原文不外泄。测试数据全部合成，位于 checkout 外。

原生 SDK 全量回归 47/47 通过（197.664 秒），包括 8 项新增回归；Python 全量契约 326/326 通过（685.613 秒）。TypeScript 严格类型检查及构建通过，Python 源码 mypy 34 文件通过。没有改动 Python 核心实现，也不宣称修复既有 tests 全库 mypy 的无关 unused-ignore。

完整配置和恢复接口见[dsh 契约](../contracts/dsh-native-v1.md#可靠文本模式m58-14)。没有声明桌面 UI、真实 provider、多插件、物理断电、恰好显示一次或性能目标通过。

## Standards

独立审查对照 AGENTS、贡献指南、当前架构、换机交接和相关契约，未发现可证实的规范违规或值得提出的基线 smell，0 项。重点核对了不确定提交保持 open、停止清理不释放尾部、code point 发布及合成数据外置。

## Spec

独立审查确认固定 SDK awaited seam、提交后发布、重建最终块、取消/截断未完成状态、持久标记发现及工具结果限制，缺失/部分实现、范围扩张和已实现但错误均 0 项。另跑两项真实 SDK 发布/失败边界验证通过；该定向复核不扩大完整测试的验收边界。

两轴发现数：Standards 0、Spec 0；两轴均无未解决问题。
