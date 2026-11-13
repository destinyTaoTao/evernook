# #120：线上原始响应留证与 DeepSeek Pro 试验

本次使用现有 provider 和固定合成语义集评估 `deepseek-v4-pro`：十例严格通过 3、失败 2、无效 5，可回答例 2/6；一次回答动作按冻结规则计错误肯定。未达到 8/10、可回答 5/6、负例无错误肯定的筛选线，#120 保持开放、日用默认关闭。无效的五例均返回 `finish_reason=length`、输出 512 token，不能推断模型最终会怎样回答。

新增的 `probes/admission/remote_semantics.py` 只接受仓库固定哈希的合成集，复用现有生产 provider 及冻结评分器，不修改语义提示、评分或生产运行代码。输出目录必须位于仓库外并新建；每次成功接收的响应原文、SHA-256、返回模型名、fingerprint、结束原因、usage、原始判断及翻译后判断均可复核。响应在落盘前检查密钥和嵌套编码；错误响应、无法解析或疑似含密钥的响应不留原文。HTTP 错误和超时可能没有响应或准确计费，不能当作免费或成功。此诊断直接对合成数据授予内部出站许可，不能证明真实 Core 的授权或检索链路。

在已配置 `DEEPSEEK_API_KEY` 的可信环境中，可用 `python probes/admission/remote_semantics.py --output <仓库外新目录>` 重跑；需将 `src` 和 `probes/admission` 加入 Python 模块搜索路径。该命令会产生真实 API 费用，每次运行都创建新的记录，不覆盖旧结果。

官方接口返回模型名均为 `deepseek-v4-pro`，fingerprint 均为 `a307abda487cd1b463329ccb945ce396`；这是供应商返回的别名和后端指纹，不是本次独立确认的固定权重版本。[官方价格和模型说明](https://api-docs.deepseek.com/quick_start/pricing/)记录 Pro 版本信息，但别名可能改变。十份原始响应和增量报告保存在仓库外 `C:/Users/dengtaotao/Documents/Codex/2026-11-13/evernook-120-deepseek-pro-raw-v1`；每份响应的 SHA-256 均已重算、可评分输出已使用原 scorer 重算。冻结集 SHA-256 为 `eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0`。

通过：link-location、tool-permission、user-confirmed。失败：assistant-guess 返回 refuse，与冻结弃答不同；reported-guess 返回 answer/“不能”，按冻结动作指标计错误肯定，但不能据此断言模型采纳猜测。其余五例达到输出上限。原始判断与经 provider 翻译的五个完整输出判断一致；没有将被截断的中间推理当作最终答案。

固定每例 30 秒、4096 输入及 512 输出 token，按官方峰时未命中价输入 $1.32／百万、输出 $3.96／百万预留，单次上限 8000 micro-USD、十例总上限 80000 micro-USD。十例返回 usage 共输入 6637、输出 3940，按配置逐次向上取整估算 24373 micro-USD（$0.024373），非供应商发票；墙钟中位数 4865 ms、最大 6049 ms，十例不能作为 p95。未改预算重试或挑选结果，无法从此实验推断更大输出预算的质量。

安全及验证：采集器含合成固定集、逐次及总费用预留、原始响应凭证检查；真实 HTTP worker 的合成回归覆盖留存、模型标识和预算不足时零请求。22 项既有/新增安全与语义测试通过；采集器新增故障路径测试及 strict mypy 另行验证。未调用真实用户记忆、未替换用户安装、未改变生产安全边界。真实端到端权限/撤回、账单与日用验收均不属于本次单轮评测。
