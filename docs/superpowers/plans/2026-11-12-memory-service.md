# 独立记忆服务实施计划

> For agentic workers, use superpowers:executing-plans task by task.

**Goal** 完成独立进程及 dsh 存取查一次切换。

**Architecture** 复用 MemoryRuntime 与原有操作，加入本机认证 RPC 和兼容客户端。服务承担后台维护，普通及可信身份分别验证。

**Tech Stack** Python 3.14、SQLite、标准库本机 TCP、HMAC-SHA256、现有 MCP、TypeScript。

**Spec** GitHub #106 与 docs/reports/architecture-review-2026-11-12.md。

## Global Constraints

单用户、本机离线、运行数据在 checkout 外。原始证据和控制可追溯。日常启用沿用既有质量门禁。正式环境一次切换全部已启用入口。每票测试、提交、推送、更新 issue。

## Review Focus

未配对请求、伪装服务、输入/响应超限、慢连接、重复请求及重启后回执；关闭宿主后的维护；管理权限与来源版本；新写后回滚；停止旧直写。

## Task 1 — #107 独立进程

- Create src/evernook/service_transport.py，帧长上限8MiB、双向HMAC挑战、整次传输截止、JSON严格解码，固定loopback。
- Create src/evernook/service.py，ServiceClient(config).call(access, operation, arguments)，复用submit/read/search/recall/status。服务固定账本，独占启动锁，认证发现文件按现有owner ACL校验；8连接上限。
- Create tests/contracts/test_service.py，真实子进程双客户端、幂等、冲突、错身份、重启、第二实例、超限、坏JSON与服务认证失败。
- [ ] 先运行缺模块回归，确认失败。
- [ ] 实现并运行 unittest test_service，以及 strict mypy 新模块。
- [ ] 新文档及源码按票提交并推送，核验远端提交。

## Task 2 — #108 统一分派与桥接

复用 mcp_server 参数模型及分派，保留 context 取消。可信 CLI 分派接收显式输入与Access，禁止请求指定文件路径。服务模式 MCP/CLI 通过发现文件连接，不可达失败而不直写。覆盖原MCP及管理测试。

## Task 3 — #109 后台生命周期

复用scheduler与navigation，服务使用独立可信维护身份与持久配置。前台优先、暂停、超时、资源、错误状态可观测。关闭客户端仍处理，服务重启补跑。未准入自动发布保持关闭。

## Task 4 — #110 dsh

MemoryClient及runLocalCli统一服务模式，Web身份必须capture+recall。测试新会话实际请求、查询、更正与抑制，页面与SDK分别验收，沿用可靠发布与#105预算边界。

## Task 5 — #111 切换

清点并阻止旧直写，排空、核对检查点与已有受保护资产保全。在新写、取消、服务重启、宿主卸载等条件验收。独立安装复核源码/产物一致。

## 后续

#112–#117 分别处理跨年召回、自动画像、信息网络、私密、销毁和异机恢复。

## 执行记录

- #107 complete，7249c3e，服务与启动7项、strict mypy3文件通过，远端提交已核对。
- #108 complete，62a5608，原回归36项、原MCP经服务20项、服务/桥接10项通过，strict mypy6文件。评审长预算缺陷已按RED→GREEN修复。真实tokenizer context取消后子进程回收通过。
- #109 已验证，服务维护/scheduler/navigation共33项、补导航配置后服务维护3项通过，strict mypy4文件。运行中暂停改为单来源/阶段/导航协作检查。语义模型周期索引留给#112。
- Ruling，传输采用标准库loopback TCP并双方HMAC认证，固定本机地址和owner-only发现文件。相对命名管道减少Windows专用封装，沿用同账户恶意程序不隔离的产品边界。
- #110 已验证，Python22项、源SDK/Web4场景、独立安装Core/Web在rc.5宿主3场景通过，49/37成员源码包实装相同，61依赖边与审计通过。CLI required发现文件竞态已RED→GREEN修复，安装和错误回执记录见service-dsh-2026-11-12.md。
