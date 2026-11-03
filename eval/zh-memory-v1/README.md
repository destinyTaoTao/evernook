# 中文记忆评测 v1：候选集

对应 [#74](https://github.com/destinyTaoTao/evernook/issues/74)。当前为 **AI 起草、待维护者人工核对的候选集**，尚未满足票据“至少 200 条人工核对查询”的冻结要求。没有模型准入、自动接纳或宿主回答成绩。

- [corpus.json](corpus.json)：136 条公开合成来源、76 条显式事实种子和 224 个唯一查询。
- [expectations.json](expectations.json)：588 个独立检查点，保存 136、检索 60、时态 108、自动接纳 60、宿主回答 224。
- [manifest.json](manifest.json)：原始 UTF-8 文件 SHA-256、版本、人工复核声明、安全 canary 和待定模型/硬件门槛；验证器将安全策略哈希一并绑定到 suite_hash。
- [REVIEW.md](REVIEW.md)：逐查询上下文、时间条件、规范答案和来源，供人工复核。

数据包含 20 个跨年更正组（每组 8 个问题），以及 64 个独立候选/问答上下文；其中另有 8 组明确未澄清冲突。来源时间从 2021-01-01 到 2026-01-05，共 1830 天。源记录和 valid time 跨五年；运行时实际录入时钟不回拨，knowledge time 用公开事实回执的 recorded_at 检查点定位，不代表系统已连续运行五年。

白名单为回答详略/排版、技术工具和一般兴趣。身份、关系、健康、财务、授权、引用、否定、角色扮演、助手猜测和混合风险分别标注。负向/混合语义保守维持审查，不把否定提炼为正向偏好。自动接纳列是后续实现的目标准则，不是现有核心行为。

独立上下文只能使用查询的 context_sources，不能将 64 个隔离场景拼成同一用户的全局偏好。跨年组的 subject、有效时间和知识检查点须一起传给宿主。检索列测试固定 exact/lexical 查询对原文的有界回取，未测试 embedding 或自然问题的完整召回质量。宿主回答采用受约束 decision/answer/source_ids；按规范答案逐字评分，不自动判断自由改写的语义等价。事实命中与宿主回答分项计分。

## 复跑

在仓库根目录，使用已安装本 checkout 的 Python 3.14 环境：

```powershell
.venv\Scripts\python.exe -m evernook.evaluation validate --suite eval/zh-memory-v1
.venv\Scripts\python.exe -m evernook.evaluation run-core --suite eval/zh-memory-v1 --output '<仓库外且尚不存在的合成运行目录>'
.venv\Scripts\python.exe -m evernook.evaluation score --suite eval/zh-memory-v1 --results '<仓库外结果文件.json>'
```

run-core 通过公开 initialize/submit/read/search/CandidateWorkflow/FactRegistry 接口运行，不读取内部表，不运行模型或自动接纳。它只生成 core-results.json 和 report.json。来源重试、提取质量、语义检索和完整宿主采集由相关契约另测；本脚本不静默代跑它们。输出目录已存在时拒绝覆盖，失败后应保留诊断并选择新的合成目录。

退出码：0 表示所提供的全部检查点通过，1 表示回归失败或输入无效，2 表示仍有缺失结果。run-core 通常返回 2，因为自动接纳与宿主结果未运行；查看每个维度的 passed/failed/missing。即使返回 0，admission 仍为 pending；它不是启用模型的信号。

## 人工复核与冻结

维护者逐项核对 REVIEW.md，必要时修改数据与期望；修改任一文件后必须重新计算对应 SHA-256，并让复核声明重新绑定新 suite_hash。Git 属性已固定 JSON 为 LF，不依赖 Windows 自动换行转换。

完成复核后，由维护者明确提供复核者标识、所有已核对 query IDs 与 suite_hash。人工复核声明需覆盖全部查询且哈希匹配，不能用采样通过替代 200 条人工核对，也不能把 AI 审查或泛化的“继续实现”写成人工核对完成。该声明记录维护者确认，不是身份认证或数字签名。模型/硬件门槛仍独立 pending。

协议与边界详见[评测契约](../../docs/contracts/evaluation-v1.md)。不要将运行库、访问材料或私人对话提交到此目录。
