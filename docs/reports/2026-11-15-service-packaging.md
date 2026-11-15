# #241：普通服务的可选保险库依赖

> 资源勘误：本文旧 PID 父链峰值归属未经验证，撤回资源验收用途。最终 wheel 在真实 site-packages、无 src 路径环境下另测 31 次 IPC/context：首次 3405.21 ms，后 30 次 p50/p95 905.81/1104.46 ms，小夹具也不能保证 1 秒。Job 监控 147 次采样、零查询失败，private 观察峰值 854126592 字节。先前一个带 src 环境的 Job 尝试已明确排除安装验证，不与新结果合并。详见[总报告](2026-11-15-autonomous-validation.md)。

非 editable Core 安装实测暴露普通服务启动失败：service 无条件读取单库绑定模块，该模块为一个有界文件读取 helper 引入 vault_migration，进而无条件导入可选 PyNaCl。只安装 Core/MCP 而没有 vault extra 的环境无法启动普通记忆服务。

修复仅将绑定的 64 KiB 上限读取留在轻量 HMAC/JSON 校验模块，保持 duplicate-key、材料结构、认证、路径权限及错误语义；不导入迁移/加密实现。未配置保险库可以启动，配置或绑定不一致仍按原规则拒绝，未改变 vault 的依赖声明。

回归通过真实子进程拒绝所有 nacl 导入，验证单写者普通服务实际启动、remember 与 read。此回归先重现原导入失败，修复后通过；与迁移/销毁联合 52 项结束（2 项 skip，未计作通过），service 独立 23 项通过，相关 strict mypy 通过。较早 88 项联合运行有一项临时目录清理遇 WinError32；该项隔离重跑与后续整组 service 23 项均通过，未将首次失败归因于旧代码或隐藏它。

隔离安装使用项目既定 MCP extra 的缓存包离线安装，未新增生产依赖。最终安装服务在 3 条合成原文上实际调用 31 次 IPC/context：首次 3642.64 ms，后 30 次 p50/p95 799.21/881.43 ms。窗口采样 130 次，Private Bytes 观察峰 852336640，3 次查询失败。此结果包含 IPC、上下文构建及 tokenizer，仍只是小夹具/并发机器负载下的采样，不能代替规模、硬峰值或宿主模型准入。两次修复前启动失败及最后成功窗口均留存于外部 campaign 的 installed-service-context 系列。
