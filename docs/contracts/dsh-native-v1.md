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
- `llm/stream` 在一次尝试内缓冲所有帧，合并检查分块/交错的凭证信号，然后原子提交正文和 `raw-stream-v1` JSON 附件；只有正文原件与完整采集均成功才向原生循环 yield 帧。**当前会等整个尝试保存后显示，尚无逐 token 实时输出。** 默认上限 768 KiB，超限阻断，不静默截取。
- 失败、取消、未闭合流保留明确状态和投影，未确认的前缀不显示；异常可能已被原生 LLM 转成 `finish(error)`。进程在提交后、显示前终止会留下已保存但未显示的尝试，不保证“恰好显示一次”。
- 原生 `tool/call` 的原始 arguments 与 `tool/result` 的文本分别保存采集信封，关联最近的真实用户输入与工具名称；失败工具保留 failed 状态。模型输出的工具帧已在进入循环/执行工具前经过上述保存屏障。派生上下文不授予执行权限。

## 附件与注入

用户文件经 `ctx.attachments.readFileStream` 读取，严格解析原生 `sha256:<64 hex>` 身份；只读取不超过 1 MiB 的 `.txt/.md/.json`。核心再次检查 UTF-8、尺寸、散列和敏感信号，并在同一 SQLite 事务保存。最多 16 个附件。图片、其他格式和超限文件不读取原件，登记 `entry_not_provided` 与原始声明元数据；这表示适配器未取得原件，不代表成功保存多模态内容。读取/完整性异常明确失败，不能从证据里提供的路径直接读文件。

召回得到候选后，重新读取仍可见的来源，展开至多 2048 个 Unicode code point 的片段，附绝对来源坐标、总长和版本，并遵守可配置字符上限。系统/开发者审计项不作为对话片段注入。跨会话例子验证“预算”可取到“预算37元”。普通证据放入原生 `contexts`，实际由 dsh 投影为 `role=user, source.kind=runtime-context`，不修改高优先级系统人设。降级、附件缺口和省略数量随证据可见；历史文字始终是不可信数据。

字符上限不是 token 预算，也没有实现 T16 的置顶、人设确认和多层上下文构造。当前查询取用户文本前 256 code point；只验证基本链路，不代表长问题的召回质量已验收。

## 重扫与边界

`installMemory(ctx, config)` 返回 `capture(session)`、`rescan(sessionId)` 和 `close()`。重扫通过真实只读 persistence handle 读取，来源冲突不会被覆盖；重连新适配器和重复重扫必须保持消息唯一。

dsh 会先留存用户输入及运行记录，插件无法撤回宿主已保存的明文，也不能覆盖其历史 UI、日志或其他插件的显示通道。凭证检测仍是有限规则；保险库未启用时，检测到的原件不保存、不注入，敏感输入/输出会明确阻断。宿主收到普通记忆后的模型出机策略由宿主负责。

真实桌面加载、系统断网、进程强杀/物理掉电、压缩/分支、用户取消及多插件组合仍须单独验收，不能用本契约的内核测试代替。
