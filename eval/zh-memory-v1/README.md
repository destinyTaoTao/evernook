# 中文记忆评测 v1：委派 AI 复核版

对应 [#74](https://github.com/destinyTaoTao/evernook/issues/74)。维护者于 2026-11-04 明确委派自主复核，主审与独立 AI 已逐条核对全部 224 个查询，按该授权冻结为 **AI 辅助复核版**；不声称人工金标。human_review 仍为 pending，ai_review 独立记录授权、两轮逐条凭据及哈希。没有模型准入、自动接纳或宿主回答成绩。

- [corpus.json](corpus.json)：136 条公开合成来源、76 条显式事实种子和 224 个唯一查询。
- [expectations.json](expectations.json)：588 个独立检查点，保存 136、检索 60、时态 108、自动接纳 60、宿主回答 224。
- [manifest.json](manifest.json)：原始 UTF-8 文件 SHA-256、版本、人工复核声明、安全 canary 和待定模型/硬件门槛；验证器将安全策略哈希一并绑定到 suite_hash。
- [REVIEW.md](REVIEW.md)：逐查询上下文、时间条件、规范答案和来源，供人工复核。
- [ai-review.json](ai-review.json)：主审与独立 AI 各 224 条复核说明，绑定相同套件身份；不是模型/宿主评测输出。

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

## 复核与冻结记录

本次冻结身份为 `b487e1777bd06eef30a50c3fc6811ef54e801c8bc00b3d46e00c6388b8d9886b`。复核覆盖每个问题的上下文、时间与知识条件、规范答案、来源及风险分类；20 个跨年组按语料中显式事实种子的有效期解释，不把本评测的时段规则当作真实抽取能力。

主审 `codex-primary` 和独立审查 `codex-independent-spec` 各有完整唯一的 query IDs 与逐项说明。加载器验证 ai-review.json 的字节哈希、套件身份、两位不同 AI 及全部检查通过；任一数据、版本或安全策略变化都使旧凭据失效。该记录是维护者明确委派后的 AI 声明，不是人类身份认证或数字签名。原人工声明保留未完成状态，模型/硬件门槛仍独立 pending。

后续如改动问题或期望，应更新对应 SHA-256 并重做受影响的复核、生成绑定新 suite_hash 的完整凭据；不能沿用旧记录直接宣布冻结。Git 属性已固定 JSON 为 LF，不依赖 Windows 自动换行转换。

协议与边界详见[评测契约](../../docs/contracts/evaluation-v1.md)。不要将运行库、访问材料或私人对话提交到此目录。
