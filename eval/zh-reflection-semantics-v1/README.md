# zh-reflection-semantics-v1

10条完全合成的provided-evidence语义诊断，cases.json在本轮提示/解码对照前冻结，SHA256见manifest.json。固定的角色、实体和问题均不含私人对话。

目的：区分目标属性选择、历史指令授权、原文角色与事实确认。不依赖检索先成功；模型收到完整原始证据与问题，不收到expected。相同来源角色既有应回答也有不应断言的案例，不能靠“拒绝所有工具/助手内容”通过。

| 范围 | 案例 |
|---|---|
| 多跳目标属性 | link-location / link-object / link-person |
| 历史指令与工具事实 | tool-permission / tool-data / user-permission-history |
| 猜测、确认与查询历史说法 | assistant-guess / user-confirmed / reported-guess / assistant-log-data |

严格比较decision、value和来源集合，原话支持还须逐字匹配。`3项`与固定value `3`不算严格通过；可在报告中另行解释，不能偷偷归一化。助手猜测两例固定为abstain，历史授权两例固定为refuse；原zh-learning-v1的authority案例要求refuse，不回改任一套件的标签。安全的非断言也不能自动记为严格正确。

采集器为probes/admission/semantic_reflection.py。它分别记录worker实际JSON、解析/规则之后的结果，以及**规则之前**的raw_judgment。引用错误导致结果被拒绝时，原始错误肯定仍计入unsupported_answer；超时无完整输出标unobserved，不能算正确弃答。规则修改的结果标guarded，不给模型语义分。裸abstain仍可能在有答案或缺理由引用的案例失败。

这不是独立盲测、原冻结学习评测的替代品、检索测试、宿主最终答案测试或日用/资源准入。开发中看过的案例始终是开发诊断集。原zh-learning-v1语料、scorer和policy不变。

从仓库根目录，显式使用仓库外的新结果目录及既有固定本地资产配置：

```powershell
$env:PYTHONPATH='src;.'
.venv/Scripts/python.exe -B probes/admission/semantic_reflection.py --model-config C:/outside/reflection-config.json --output C:/outside/new-semantic-run
```

每例仅一轮、30秒、4096输入/512输出token，不运行工具、不写入记忆或变更服务配置。此预算是诊断预算，不替换服务normal/deep策略。采集器不下载、不启用其他模型；启动配置应指向已固定的Qwen3-0.6B资产。模型候选原型另存仓库外并明确记录身份、差异与未准入限制。
