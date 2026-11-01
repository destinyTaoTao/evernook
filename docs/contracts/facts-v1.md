# T13 显式有来源事实与时间语义增量

对应 [#14](https://github.com/destinyTaoTao/evernook/issues/14)，`evernook.facts.FactRegistry` 接受已完成 [候选任务](candidates-v1.md) 的结果，由可信本地调用端提交 `FactDecision`。它没有启用抽取模型或自动接纳，也不修改人设文件或高优先级指令。

## 决定与版本

`FactDecision` 必须包含稳定 `request_id`、明确的 `subject_id/predicate`、`candidate_id`、有效用户 `confirmation_id`、`expected_revision` 和 `kind`，可选显式时间、`replaces_id`、`resolves`。不同同名主体由调用端提供不同ID；不会仅凭名字合并。

- `assert`：该主体/属性的首个现实陈述，已有现实记录时拒绝隐式覆盖。
- `correction`：纠正指定事实的最新版本，保持其 valid_from/valid_until/occurred_at；不将“当时说错了”伪造成今天发生现实变化。也可更正已经结束的旧时段，不影响后续搬迁时段。
- `change`：明确的现实变化，替代最新现实节点，必须给有效起点且晚于该节点的已知起点。旧记录不重写，查询将新起点作为旧时段的有效上界。
- `plan`：独立计划，必须给预期起点；即使日期已过也不变成现实事实。
- `disputed`：保存待澄清说法，查询返回争议集合、原有陈述及 `clarification_required=true`，不根据最后写入者选择真相。

现实决定可用 `resolves` 显式关联待办计划或争议。解决本身追加新决定，不删除旧证据；不可重复解决或跨主体/属性解决。v1现实变化仅支持按有效起点递增追加；复杂回填/重叠时间段明确拒绝，不静默重排。

同一键的所有决定（含计划/争议）共享递增revision；后台旧版本或并发不同决定只有预期版本匹配者成功。相同request_id、相同内容重试返回原回执；同ID异内容冲突。候选来源、片段、完成状态、用户来源类别、确认记录和CAS在同一SQLite短写事务中检查/提交。助手生成、引用/角色扮演、争议候选的直接现实接纳会被拒绝；争议候选只能记为disputed。

这里的“确认”是可信调用端提供的用户证据引用。身份和来源校验不能从文本证明用户真的同意了该决定；宿主仍须建立实际用户意图。该写入接口不能直接交给普通模型，`approved=true`不是权限。当前CLI仅为可信本地维护入口。

## 时间与可见性

`query(access,subject_id,predicate,valid_at=None,known_at=None)` 分开处理现实有效时点与本模块已经记录的知识时点：

- `valid_from/valid_until` 为明确有效范围，`occurred_at` 为显式事件发生时间，不从写入时间推断。
- `learned_at` 保留所用交流来源时间的上界，任一来源时间未知则为未知；`recorded_at` 是本模块接纳的系统时间。确认原话和时间可通过confirmation_id回查。
- `known_at` 按recorded_at选择当时已经接纳的版本，可回看更正前的说法；同一 subject/predicate 的 recorded_at 水位单调，墙钟倒退不会把新决定记到旧时间之前（避免构造从未存在的知识视图）。不能穿越当前抑制/撤销规则读取被禁止的证据；可信维护端同 ID 重试返回原回执属于既有确认语义，不重验当前来源可用性（与 experience 的重验策略不同，属有意的信任边界差异）。
- `effective_until` 是所选知识范围内由后继现实变化/显式上界计算的实际结束时间；不回写旧FactRecord。
- 未知起点的导入记录在当前查询保留 `unknown_time` 标识；指定历史时点不能把它冒充当时有效的事实。日期都需显式时区offset，未借此确认产品默认时间区。

查询先选时间链/最新纠错，再检查所选来源与确认的当前状态；新事实来源不可用时返回 `source_unavailable`，不重新选旧事实冒充当前。争议/计划保留各自来源；返回candidate/processor版本、原文片段坐标及确认版本。原文保持不可重写，派生文本仍是不可信数据。单键超过10000个决定明确报 `fact_scope_too_large`，不返回伪装完整的截断结果。

确认删除来源或确认记录时，事实正文及来源片段清除，但无正文时间控制与修订继续保留；当前值和最新纠错不可用时仍返回 `source_unavailable`。历史查询只能取得当时有效且当前仍可读取的事实，不能复活已删除的纠错正文；未受影响的早期现实变化时段仍保留正确上界。删除不回退 CAS 修订，已删除决定的请求 ID 重试或复用得到 `fact_source_purged`。

## CLI 与 MCP

`fact-decide --config ... --access ...` 从UTF-8 stdin接收完整决定JSON，`resolves`用数组。`fact-query` 从stdin接收subject_id、predicate及可选valid_at/known_at。它们不执行抽取或猜测主体ID。

默认查询MCP新增 `evernook_v1_facts`，只提供上述读取；可信/普通profile都没有 `fact_decide` 工具。聊天模型不能用查询参数接纳事实或更改人设。`recall` 继续表示原文候选融合；完整事实/人设/经历上下文构造留给T16。

普通归档会复制这些同库派生表，但恢复仍为review_required；尚未实现最新控制重放后的正常运行库发布。没有把该增量、合成用例或合法JSON输出当作T12抽取模型质量准入或T13全部产品验收。
