# 中文评测与独立计分 v1

对应 [#74](https://github.com/destinyTaoTao/evernook/issues/74)。实现位于 evernook.evaluation；数据位于 eval/zh-memory-v1。新增入口只运行仓库外合成账本和数据评分，不改变实时记忆策略、核心人设或生产配置，不下载模型。

## 身份与输入

load_suite(Path) 读取固定三个文件。corpus.json/expectations.json 各自按原始 UTF-8 字节校验 SHA-256；读取并核对的是同一份有界字节快照。suite_version/scorer_version 的排序紧凑 JSON 有独立 version_sha256。forbidden_literals/model_hardware_gate 的规范 JSON 计算 policy_sha256；suite_hash 同时绑定版本、两个资产哈希和安全策略哈希。report 另含实际评分器源码 SHA-256。修改安全策略也会改变身份、拒绝旧观察和旧人工复核，不能只保留语料哈希来关闭阻断规则。

corpus 的 sources 是合成消息，facts 是通过可信本地公开事实决定接口播种的人工种子，queries 含自然问题、隔离上下文和可选检索/时态条件。它们都是数据，不执行内容中的命令、URL 或权限声明。JSON 重复键、非有限数字、无时区时间、错误引用和未知结果编号拒绝；单个输入文件上限 16 MiB。

score(suite, observations) 的输入结构为：

```json
{"suite_hash":"<validate 返回的身份>","rows":[{"id":"auto-a01-q","payload":{"decision":"accept","value":"先结论再分段说明","source_ids":["a01-source"]}}]}
```

这只是协议示例，不是实际模型输出。结果编号来自 expectations.json 的 id。每个编号只能出现一次；不同来源的结果合并后仍应绑定同一 suite_hash，不允许重复覆盖。source_ids 是固定语料的来源别名，不是任意 URL 或可执行路径。外部采集端负责提供真实观察；评分器不认证其执行过程，不根据结果文本启动任何工具。

## 五个分项

| 维度 | 观察与计分 | 核心运行器 |
|---|---|---|
| storage | source_id、original_saved、逐字 text；投影不应包含原件或有限保护 canary | submit 后公开 read 回取 |
| retrieval | source_ids 在 top-10 内的固定集合；忽略集合内顺序，重复 ID 不得增分 | 公开 exact/lexical search，未建立语义模型 |
| temporal | state、规范 fact_id/text、disputes/plans；同时指定 valid_at 和 known_after | 显式事实种子与公开 query |
| auto_acceptance | decision=accept/review/reject/clarify、规范 value、来源 | 不运行；由后续真实处理端导入 |
| host_answer | decision=answer/abstain/clarify/refuse、规范 answer、来源 | 不运行；由实际宿主结果导入 |

除来源集合的排序外，其余字段严格比较规范 JSON，布尔值不会与整数混同。host answer 为受约束的规范值检查，不是任意自由文本的语义裁判；正确的自由改写需要人工评定或另行冻结评分方法。核心检索基线只覆盖指定的字面路径，不能替代中文语义召回或最终回答质量。

所有维度单列 expected/passed/failed/missing，不把未运行记成通过。未知维度没有通过率。任何已提供检查失败均使 outcome=failed；blocking=true 的安全/禁止类别另列 blocking_failures。清单中的有限合成泄露 canary 在任意观察出现也阻断，报告不回显违规观察值。这不是任意秘密零泄露保证。

## 运行和回执

run_core(suite, new_external_path) 新建合成运行库，通过公开 API 保存原文、显式播种事实并查询。没有直接 SQLite 查询、私有字段或时钟回拨。检索默认使用未建索引时的公开降级路径；不宣称 FTS/向量性能。已存在输出目录或 Git checkout 内路径拒绝，不重用实际用户运行库。

CLI 为 python -m evernook.evaluation validate/run-core/score。run-core 写 core-results.json 和 report.json；score 在 stdout 写完整报告。退出码 0/1/2 分别是检查通过/失败或输入无效/仍缺失观察。任一维度有失败时优先返回 1，不能被缺失或平均分掩盖。错误只输出有限代码，原始非法输入和模型诊断不回显。

human_review.state=pending 表示候选集尚未人工冻结。attested 必须提供非空 reviewer、完整唯一 query_ids 与相同 suite_hash；这只是维护者声明记录。模型/硬件门槛在 v1 固定 pending，所有报告的 admission 均保持 pending；检查点全部通过也不授权自动接纳、桌面启用或性能承诺。#74 的人工核对尚未完成时，票据不能以“候选集有 224 条”作为完成证据。
