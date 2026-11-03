# T10 dsh 原生适配增量

对应 [#11](https://github.com/destinyTaoTao/evernook/issues/11)。实现位于 `plugins/harnesses`，固定 dsh `0.2.1-rc.3`、Cordis `4.0.5`、Node `24.11.x`；通过 MCP TypeScript client `2.4.0` 连接同一 Python 账本。测试组合真实的 Session、JSONL persistence、AgentLoop、LLM、Tools 和 Attachment 服务，模型使用合成离线适配器。

## 接线与权限

Python 环境安装 `.[mcp]`；在插件目录执行 `npm ci --ignore-scripts`、`npm run typecheck`、`npm run build`。由可信宿主配置其已有 Cordis 上下文：

```ts
import memory from '/absolute/path/to/evernook/plugins/harnesses/dist/dsh.js';

await ctx.plugin(memory, {
  python: 'E:/dedicated-venv/Scripts/python.exe',
  config: 'E:/configured-memory/runtime.json',
  access: 'E:/configured-memory/mcp-access-dsh.json',
  entry: 'dsh',
  maxStreamBytes: 786432,
  maxEvidenceChars: 12000,
}).await();
```

路径为占位示例，运行目录和访问文件必须事先由本地操作端初始化、配对并通过 ACL 检查。`entry` 必须匹配该身份的登记；不将凭证传给模型。可选 `embeddingConfig` 是仓库外的明确本地模型配置，缺省走字面召回并报告降级。插件使用 `--profile trusted-host`，其写入工具只在私有宿主连接中使用，不能把该连接直接作为聊天模型的工具集。

这是 Cordis API 接线方式；尚未验证已安装桌面应用的插件加载、自动发现或升级。插件不修改用户现有 dsh 配置、会话、模型或数据目录。T09 完整恢复及日常启用准入仍然适用。

## 输入、流与工具

- 在 `system-prompt/assemble` 和 `agent/request` 阶段先 flush 原生 Session，再保存真实 `source.kind=user` 输入；保存失败抛错，阻断 provider，不静默继续未保存的交流。未提供“继续未保存”按钮。
- 来源使用绑定的入口、Session ID、分支 ID 和原生 seq/index；另存 header 与事件审计。分支 header 保留 parentSession；继承前缀按子分支记录，不推断为新的当前事实。温态水位保存在内存；冷启动重扫依靠核心来源幂等，未声称有持久化扫描游标或有界启动成本。
- 默认 `streamMode='buffered'` 在一次尝试内缓冲所有帧，合并检查分块/交错的凭证信号，然后原子提交正文和 `raw-stream-v1` JSON 附件；只有正文原件与完整采集均成功才向原生循环 yield 帧。此兼容/验证模式等整个尝试保存后显示，不属于可靠逐块展示验收。显式可靠文本模式见下文。默认上限 768 KiB，超限阻断，不静默截取。
- 失败、取消、未闭合流保留明确状态和投影，未确认的前缀不显示；异常可能已被原生 LLM 转成 `finish(error)`。进程在提交后、显示前终止会留下已保存但未显示的尝试，不保证“恰好显示一次”。
- 原生 `tool/call` 的原始 arguments 与 `tool/result` 的文本分别保存采集信封，关联最近的真实用户输入与工具名称；失败工具保留 failed 状态。模型输出的工具帧已在进入循环/执行工具前经过上述保存屏障。派生上下文不授予执行权限。

## 附件与注入

用户文件经 `ctx.attachments.readFileStream` 读取，严格解析原生 `sha256:<64 hex>` 身份；只读取不超过 1 MiB 的 `.txt/.md/.json`。核心再次检查 UTF-8、尺寸、散列和敏感信号，并在同一 SQLite 事务保存。最多 16 个附件。图片、其他格式和超限文件不读取原件，登记 `entry_not_provided` 与原始声明元数据；这表示适配器未取得原件，不代表成功保存多模态内容。读取/完整性异常明确失败，不能从证据里提供的路径直接读文件。

召回得到候选后，重新读取仍可见的来源，展开至多 2048 个 Unicode code point 的片段，附绝对来源坐标、总长和版本，并遵守可配置字符上限。系统/开发者审计项不作为对话片段注入。跨会话例子验证“预算”可取到“预算37元”。普通证据放入原生 `contexts`，实际由 dsh 投影为 `role=user, source.kind=runtime-context`，不修改高优先级系统人设。降级、附件缺口和省略数量随证据可见；历史文字始终是不可信数据。

以上字符路径保留为 `legacy_character_cap` 兼容模式。显式提供 `contextConfig` 与 `memoryBudget` 可使用[完整核心与真实目标 token 预算](host-token-budget-v1.md)，包括置顶、人设、画像、指定事实与证据，以及长查询省略披露。兼容路径仍只验证基本链路，不代表长问题的召回质量已验收。

## 重扫与边界

显式 `installDshMaintenance` 可接入[确认式维护](host-maintenance-v1.md)：模型只能提议，可信宿主展示预览，后续同会话真实用户输入确认后由独立维护身份执行。重要原话分页、来源更正/抑制、画像修订/确认和既有事实候选更正复用核心；普通采集身份不提升权限。

`installMemory(ctx, config)` 返回 `capture(session)`、`rescan(sessionId)` 和 `close()`。重扫通过真实只读 persistence handle 读取，来源冲突不会被覆盖；重连新适配器和重复重扫必须保持消息唯一。

dsh 会先留存用户输入及运行记录，插件无法撤回宿主已保存的明文，也不能覆盖其历史 UI、日志或其他插件的显示通道。凭证检测仍是有限规则；保险库未启用时，检测到的原件不保存、不注入，敏感输入/输出会明确阻断。宿主收到普通记忆后的模型出机策略由宿主负责。

真实桌面加载、系统断网、进程强杀/物理掉电、压缩/分支、用户取消及多插件组合仍须单独验收，不能用本契约的内核测试代替。

## 可靠文本模式（M58-14）

[#72](https://github.com/destinyTaoTao/evernook/issues/72) 增加显式 `streamMode: 'durable_text'`。固定 `dsh-agent-loop@0.2.1-rc.3/lib/index.js` 的循环先等待 `for await` 获取帧，再调用 `live.push(chunk)` 发布 `agent/assistant-stream`；适配器在 `llm/stream` 的每次 `yield` 前等待核心 `capture_chunk` 回执。因此 provider → SQLite FULL 提交 → 原生发布是可等待的顺序。SDK 当前[公开流契约](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md)说明中间件语义；本票以本地锁定版本源码和真实 SDK 合成测试为准，不把 master 文档当作固定版本运行证据。

仅支持 index=0 的一个文本块：block-start、text-delta、内容等于累计 delta 的 block-end，最后 stop 或 max-tokens。每个 provider delta 可再按 64 KiB UTF-8 分块；跨 delta 的 UTF-16 代理对在传输前拼合。可见 text-delta 只包含核心回执的 `released_chars`，未消歧尾部保留在进程内，绝不直接发布原始 block-end。正常 stop 才释放剩余安全尾部、建立完整回答并发布重建后的 block-end；max-tokens 保留失败/未完成状态和未发布尾部。usage 与 replayState 等原始元数据不在文本契约内，不转发。整次帧 JSON 仍受 maxStreamBytes 约束，超过上限保留已提交部分并阻断后续发布。

工具调用、reasoning、多文本块、其他索引或不一致最终块会在未经验证内容发布/工具执行前拒绝，保留已提交文本。原生 `dsh-tools` 的最终内容变换发生在可扩展 post-execute 之后，随后 `tools/result` 观察者不会被等待；因此这里没有声明通用工具结果保存屏障。维护提议工具等需要工具的调用继续使用 buffered 兼容模式，不能同时声称已通过本模式的逐块验收。

provider 错误、取消或消费者关闭时尽量以空文本终止核心流；不释放待消歧尾部。正在提交时不取消 MCP 请求，收到回执后再次检查取消信号；若传输结果不确定，保留 open 供持久回查，既不以新请求覆盖同一块号，也不自动重试 provider。每次尝试使用 UUID 和固定来源/时间，开始标记先于数据块持久化；核心仍校验来源占用和重复块。native assistant/message 只留受保护事件审计，不再另行导入为普通助手回答，避免把失败/截断前缀变成完整答案。此模式也不把启用前的历史 native assistant/message 自动导入为完整原文；其原始记录仍在原生会话中。

`installMemory` 的返回接口新增：

- `streams(sessionId, offset=0)`：从账本分页搜索持久化尝试标记，返回当前入口/会话的 `streams: Source[]` 和全局 `next_offset`。应跟随 next_offset 到结束；其他会话可能使当前页为空。开始后尚未成功起流的尝试也可能出现，readStream 会明确失败。
- `readStream(source, cursor=0, limit=2048)`：复用核心已授权回放，仅返回已提交正文、完整性和未保存尾部长度。强杀后无需原生 assistant settlement 即可找到已显示的流；rescan 只补扫原生记录，不从缺失尾部伪造完成。
- `status(sessionId)` 披露 `stream_mode` 和 `tool_publication`；durable_text 为 blocked，buffered 为 buffered_calls_results_unverified。

可信 renderer 必须显式显示未完成状态，并维护自己的 code point 显示游标；API 不会自动把旧文本重新推入原生循环。提交后显示前强杀可以留下已存但未显示的文本，不承诺恰好显示一次。新进程只回放，不续写丢失 carry 的旧流；后续 provider 请求建立新尝试。来源抑制/删除遵守核心控制，回放不能绕过它们。

已验证范围为真实 SDK 发布事件、新 MCP 客户端、取消、provider 失败、数据库中途写入失败、强杀 SDK 子进程、重复重扫、敏感尾部及 Unicode 分页；不代表桌面 UI、真实 provider、多插件、物理断电或性能验收。
