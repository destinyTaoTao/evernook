# #120：显式关闭 thinking 的线上对照

固定冻结语义集、现有消息/最终校验、30 秒、4096 输入和 1024 输出 token；在**仅处理固定哈希合成集的评测采集器**中按 [DeepSeek 官方接口](https://api-docs.deepseek.com/guides/thinking_mode/)增加 `thinking.type=disabled`，分别对 Pro 和 Flash 完整运行两轮。其余请求字段不变，无新提示、例子、语义规则或评分改动。这一诊断参数在生产 provider 中尚不可配置，实验结果不能当作实际产品配置或日用准入。

| 模型／条件 | 通过／失败／无效 | 可回答通过 | 已观察错误肯定 | 输入／输出 token | 估算 micro-USD | 中位／最大 ms |
| --- | --- | --- | --- | --- | --- | --- |
| Pro 关闭 thinking A | 6／4／0 | 4/6 | 0 | 5847／289 | 8869 | 1201.5／1766 |
| Pro 关闭 thinking B | 6／4／0 | 4/6 | 1 | 5847／296 | 8897 | 1203.5／1360 |
| Flash 关闭 thinking A | 7／3／0 | 5/6 | 0 | 5857／282 | 2105 | 965／1303 |
| Flash 关闭 thinking B | 7／3／0 | 5/6 | 1 | 5857／302 | 2129 | 831.5／957 |

原默认 thinking 对照两轮：Pro 严格 7/10、5/10（后者两例截断），Flash 均为 6/10；详细用量及逐例见[前次记录](reflection-remote-repeat-2026-11-14.md)。关闭 thinking 的四组均未截断，但仍未达到至少 8/10、可回答至少 5/6、负例无错误肯定的联合筛选线。

四组的工具数量均答“3项”，冻结值是“3”；助手猜测均输出 refuse，冻结动作要求 abstain。reported-guess 的 A 轮均输出 abstain 但漏掉所需证据，B 轮均用 answer 表示“不能”或“不能证明”，按冻结动作计错误肯定。自然语言否定不等于模型承认猜测为事实；但证据及动作不符契约，不能因此调整 gold 或自动改写结果。Pro 另两轮都将地点答成更具体的柜名而不匹配冻结值。

四组报告的 completion_tokens_details 未包含 reasoning_tokens 字段；不把缺省字段当作供应商提供的零值。所有响应的 finish_reason 为 stop，原始判断与翻译后判断重算一致；模型别名/fingerprint 分别为 `deepseek-v4-pro` / `a307abda487cd1b463329ccb945ce396` 和 `deepseek-flash` / `bc19ba97cbe016479903222bb1578db6`，不是固定权重保证。每条件仅两轮，模型服务默认采样；更低输出量、费用和时延是本批观测，不保证普遍成立，也不是 p95。

费用按峰时缓存未命中输入/输出单价 Pro $1.32/$3.96、Flash $0.30/$1.20／百万 token 逐次向上取整，本批合计 22000 micro-USD（$0.022），不是供应商账单。每个 Pro 轮次本机预留 94630，上限 100000 micro-USD；Flash 每轮预留 24580，上限 30000；超限先阻止派发。真实账单、服务端工作与供应商隐含计费不能由本机预留保证。

四组各 10 份原始响应及增量报告分别位于仓库外 `C:/Users/dengtaotao/Documents/Codex/2026-11-13/evernook-120-pro-no-thinking-a-v1`、`evernook-120-pro-no-thinking-b-v1`、`evernook-120-flash-no-thinking-a-v1`、`evernook-120-flash-no-thinking-b-v1`（后三项同一父目录）。40 个响应 hash、返回模型、冻结 scorer 原始/翻译后判断和密钥检查均复核，报告记录配置模式与实际发送请求体的规范化 SHA-256，不记录密钥。采集器通过限定枚举在测试 worker 的请求体中插入单一固定字段；发送前保留已有 provider 校验、超时和预留，loopback 集成测试检查实际字段与预算。模式是诊断专用，直接授予内部合成证据许可，不能证明真实 Core 授权、撤回或宿主链路。

未发送真实记忆，未更改生产 provider、冻结集、评分器或用户安装。没有候选通过质量门槛，#120 保持开放、日用默认关闭；实际供应商费用、正式性能、长跑及真实端到端安全尚未验收。
