# T01 补充：显示前持久化屏障

日期：2026-10-29。用户在 T04 取舍中确认：**显示过的流式片段必须可恢复，不能仅按最终消息回扫。** 本补充沿用 [T01](r0-dsh-2026-10-29.md) 的真实内核、合成数据与原生外部行为边界，为这个硬要求验证最小可行落点；不改变旧负面证据，不启动 R1 产品或接入日常 dsh。

## 实现与证据边界

固定 dsh `0.2.1-rc.3` 的公开 `llm/stream` waterfall 允许异步包装生成器。`installDisplayBarrier` 为每个真实 AgentLoop 模型请求创建独立 attempt，按 ordinal 记录每个原始 chunk；SQLite WAL/FULL 的自动提交完成后才向下一层 yield。dsh 的 `LiveAttempt.push` 随后才发布原生 `agent/assistant-stream` chunk 事件，正常界面消费这个事件后才能显示。

实验复用既有收件库连接，新增 chunk 表，未增加第二个独立存储服务。没有使用上游仅作可丢弃状态的 ignorable plugin record 保存唯一片段。片段 attempt ID、顺序与 provider finish 分开；没有 finish 的中断流保持未完成，不拿片段拼接冒充完成消息。

新增验证：

- 原生实时 text-delta 事件内，通过独立同步只读连接核对同一文本已可读，覆盖换行、数字和 emoji。
- 首个已显示片段出现后强杀子进程，重启后直接读取 journal 可取得完整片段和连续 ordinal，宿主最终消息日志仍无该片段且无 finish，证明恢复来自显示前的提交。
- provider 已开始但 journal 连接故障时，不发布任何未保存 text-delta，错误可见；原文不回退到仅内存保存后继续显示。
- 保留原始无屏障强杀用例，继续证明现成宿主最终消息重扫不足以满足新要求。

检查入口：[display-barrier.ts](../../probes/dsh/src/display-barrier.ts)、[display.test.ts](../../probes/dsh/test/display.test.ts)、[crash.test.ts](../../probes/dsh/test/crash.test.ts)。完整探针会统一运行这些与既有用例，原始证据、清单、依赖/源码哈希保留在仓库外，最终运行路径和回执在本报告下方记录。

## 后续约束

本轮仅证明固定内核组合的原生实时发布前可加可靠屏障，没有检查实际桌面像素、断电、受保护片段加密、恢复 UI 回放、跨进程服务传输或任意第三方插件重写。生产适配器必须保证屏障之后没有另一路可注入未提交片段的展示通道，并验证重试/取消/分支/多 provider 及恢复回放。

敏感分流必须在生产 journal 之前完成，凭证保险库未启用时不能把敏感原文复制进这个合成实验的普通 SQLite 表；本探针仍只接受源码里的合成文本。性能影响需要在生产路径与目标负载上另测，不能因为该方案可行就承诺显示延迟或宣布日常 dsh 已可靠。

## 验证回执

`npm run typecheck` 通过；完整探针 16 项通过、0 失败、0 跳过，包含原有负面用例和新增 3 项显示相关验证。原始证据目录为 `E:\evernook-lab\r0-dsh-20261029\run-03-display`，清单记录 dsh/Cordis 版本、来源/测试文件 SHA-256、Windows/Node 环境与运行命令；运行数据全部在 checkout 外。没有修改已安装 dsh 或日常会话，没有部署或推送。

## Standards

独立静态审查 0 项确认问题：复用现有 SQLite、合成数据、独立读取与失败/强杀边界符合仓库要求。只读核对原始回执，未重跑测试或桌面 UI。

## Spec

独立静态审查 0 项可操作问题：提交后 yield 的顺序、事件时刻读取、失败阻断及未完成片段恢复覆盖本次 R0 落点；桌面像素、敏感分流、恢复回放和插件组合仍未验收，不扩大为 R1 准入。

Standards：0 项确认问题，最严重问题无；Spec：0 项可操作问题，最严重问题无。
