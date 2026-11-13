# T16 显式预算的证据上下文构造增量

## 独立服务调用：CLI、Python SDK 与 API

独立服务以可信启动参数 `--context-config` 设置目标 tokenizer 和上下文策略后，已配对且具有 recall 权限的调用端可以通过 CLI `context`、Python `ServiceClient.context` 或认证服务的 `mcp/evernook_v1_context` 获得相同的版本化上下文包；TypeScript SDK 的 `MemoryClient.context` 继续使用同一路径。CLI 接收 UTF-8 JSON 标准输入，例如：

```json
{"query":"合成：上次聊到什么？","model_id":"configured-model","max_tokens":512,"as_of":"2026-11-14T00:00:00Z","mode":"normal","timeout":30}
```

```powershell
Get-Content -Raw ./synthetic-context-request.json | python -m evernook.cli context --config C:/outside/runtime/runtime.json --access C:/outside/runtime/mcp-access-owner.json --require-service
```

Python 调用为 `ServiceClient(config).context(access, query, model_id=..., max_tokens=..., as_of=...)`，并支持 `current_input_id`、`query_intent` 和取消。命令只通过独立服务运行；服务缺失、未配置上下文或身份不具备权限时失败，不接收调用方提供的 tokenizer、策略或模型路径。上下文包是宿主应放入低信任数据区域的背景，接口不授予记忆修改或工具执行权限。此接口连通不代表完整宿主 token 计数或日常质量门禁已通过。

`evernook.context.ContextBuilder` 组合显式必读、时态事实/未完成计划/争议、显式选择并经用户审查的经验及相关原文。所有输出都标为 `untrusted_memory_context`，不修改人设、执行规则或工具权限。[dsh/pi 目标 tokenizer 模式](host-token-budget-v1.md)复用该核心，并通过受信设置配置的只读 MCP `context` 工具调用；两个入口的字符兼容模式另行标识。

## 必填策略与真实计数

构造器要求同一个 `MemoryRuntime/SemanticRecall`、`ContextPolicy` 和宿主提供的 `TokenCounter`。策略必须明确普通/深入token额度、必读额度、自适应近期起始天数和目标内容量；必读ID和事实subject/predicate键由可信配置选择，不从召回文字中接受设置变更。

`TokenCounter.count(text)` 必须使用宿主目标模型的本地tokenizer，`counter_id`表明计数规则；核心没有把字符数、字节数或来源条数冒充tokens的降级路径。计数针对**整个最终序列化记忆payload**，不只正文；不包含宿主其他对话、聊天模板或生成额度，宿主仍须保留那些空间。测试中的codepoint counter仅是合成计数夹具，不能作为生产token估算器。

`build(access,query,as_of=...,mode=...,timeout=...,known_at=None,cancel=None)` 要求明确带时区offset的参考时间、normal/deep模式和请求预算；query最多256个code point，超限拒绝。没有把此前提出的Asia/Shanghai或4000/16000/1000建议值视为用户已确认的默认值；相关正式选择仍待配置/确认。

## 顺序、来源和省略

1. 按配置顺序取得必读原话，检查独立必读额度。单项超过4096 code point的内联范围时记录 `important_requires_paging`，保留逐页精确回取入口，不能将截断说成完整重要交代。
2. 按明确事实键查询有效/知识时间，保持现实、计划和争议分开，附来源、candidate/processor及确认版本；事实仍为数据，不变成人设指令。
3. 按可选 experience_ids 纳入当前 enabled 的[经验](experience-v1.md)，其正文仍不授予执行权限；未确认/停用/争议项省略，最终快照再次检查修订。known_at 历史查询省略当前经验并说明历史能力缺失。随后召回相关原文，附通道、词法/语义水位、近期扩展及降级状态；重新读取来源，展开至多2048 code point的有界片段，明确实际坐标、总长和bounded_excerpt标记。不将system/developer审计作为对话注入。
4. 返回前用同一可见性快照检查全部来源状态与版本。构造期间更正/抑制/删除的材料会移除，不通过派生上下文绕过。
   用户画像还检查当前 confirmed 状态及精确 revision/content_revision，来源和确认版本进入同一最终快照；已删除标签按遗漏处理。画像版本元数据计入画像和全局预算。该检查不撤回此前已经交付的上下文，人设保留自身独立确认语义。
5. 按完整payload计数，超限从末尾移除整个相关项，保留必读优先顺序。必读仍可能因全局额度不足被明确省略；不是无条件承诺所有置顶都能放入。所有省略ID/原因在sideband，payload含省略数量，`complete=false`。连框架元数据都超限时返回空payload及 `context_envelope_over_budget`。

返回 `ContextBundle` 给出payload、实际计数/上限、counter_id、模式/参考时间、省略、降级、完整状态与证据数。宿主必须把payload放在低信任数据位置，不应直接拼入高优先级系统指令。当前不自动猜跨轮代词或同名主体，调用端需给出明确查询/事实键。

## 取消与超时的实际边界

共享协作预算贯穿近期扫描、语义模型及向量扫描；取消传到MiniLM子进程，超时/取消会kill并回收进程与管道，未验证的后续命中不返回。已有核验必读/事实可作为部分上下文返回，携带对应降级，而不是伪造无答案。

数据库/身份/ACL调用仍有自身超时，已开始提交不被异步打断；宿主token counter是同步可信组件，必须自行保持有界。最终可见性检查、认证和payload计数不以返回旧内容换取硬截止。因此这是合作取消/部分结果实现，**不是严格端到端1秒墙钟或p95验收**。请求期间不启动自动抽取或后台沉淀。

嵌入后端接口增加可选 `cancel`；自定义backend须接受并正确处理这个参数。普通MCP/CLI不接收或持有Python Event，默认调用保持兼容的无取消行为。

dsh/pi 已有显式本地目标 tokenizer 接线和每轮构造注入；真实部署的 tokenizer/provider 模板、多轮主体解析、性能及系统断网验收仍需后续工作。当前构造器没有启用日常 profile。

## Current-input receipt scope (#85)

`ContextBuilder.build`, `HostContext.build` and MCP `context` accept optional
`current_input_id`. The trusted adapter supplies the current capture receipt, not a text
fingerprint. Exact, lexical, semantic and recent candidate selection exclude that ID before
ranking/limits; derived context items that depend on it are also omitted (`current_input`).
`SemanticRecall.recall` and MCP `recall` share that optional scope. No global visibility,
original record or earlier identical input is changed. With the next turn's receipt the
previous input becomes eligible again. Omission does not grant permission to publish or
weaken native input capture, protected-history screening, source versions, cancellation
or independent full-request budget checks.
