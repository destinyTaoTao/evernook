# EverNook · 忆栖 — dsh 记忆管理预览

preview.28 与包含 #126、#127 的 Core 0.1.23 配套：关系页可切换已确认画像、当前事实及已发布观察，分别回看来源、支持依据和反证；事实争议和观察推断会标注。事实与观察的搜索、分类、跨层合并图及自动实时刷新尚未实现。新接口未在旧 Core 0.1.22 中提供，须更新并重启独立服务；旧版不会自动升级。该组合尚未完成真实安装浏览器验收或日用准入。

preview.26 增加“记忆关系”视图：展示当前分页内已确认画像与来源的关联，支持筛选和分页，点击来源走原有实时可见性检查。它不是全库网络，也未覆盖事实、主题或反思；其他入口造成的变更需刷新页面后更新图上标签。合成宿主浏览器已检查中英文、空态与窄屏；尚未完成独立安装和实际 dsh 浏览器验收。

preview.25/Core0.1.22增加显式本地/线上反思provider。默认仍关闭；线上另需可信reflectionAllowRemote选项、服务端客户端allowlist及MCP启动门，key只在服务环境配置。SDK/API/CLI共用同一证据/权限/预算。没有自动云回退或真实线上模型准入，配置与费用边界见[provider契约](../../docs/contracts/reflection-providers-v1.md)。

一级记忆页面与会话右侧栏，复用同一 Python/SQLite 证据账本。提供画像/兴趣/时间线、来源分页、版本历史、更正、移出画像、来源抑制与草案接纳/拒绝/重审。页面变更先预览，用户确认后保存，模型无管理凭证。

**预览目标：Node 24、dsh 0.2.1-rc.5、Windows本机。** 未声明其他版本兼容或正式日用准入；文本可靠流模式拒绝工具/reasoning/多文本块，buffered 模式整段保存后显示。原生工具屏障需显式启用下述独立宿主组合；#94/#81/#82继续适用。

preview.24/Core0.1.20增加默认关闭的`reflection: true`实验选项，注册`evernook_memory_reflect`只读深查工具。服务需另行以可信`--reflection-config`配置固定本地模型，并用可信CLI`reflect-configure`显式启用水位；普通身份不能配置或选择模型路径。预算/配置与未通过的模型质量见[反思契约](https://github.com/destinyTaoTao/evernook/blob/codex/dsh-memory-preview/docs/contracts/reflection-v1.md)。普通每轮不会自动调用，结果是临时低信任推断，不能作为事实、画像或外部动作授权。durable_text会拒绝工具调用，实际深查应使用既有buffered或固定补丁durable_tools组合；这不改变原有发布门禁。

## 构建与隔离安装

当前Web仍是字符兼容路径，尚未接入SDK的目标tokenizer/完整请求预算；见[#105](https://github.com/destinyTaoTao/evernook/issues/105)。preview.21起页面披露实际预算模式，未知状态不按目标模式展示。字符上限不是真实token预算，也不等于正式日用准入。

Python 3.14 环境须安装本次提交的 Core（包含 `manage` CLI），例如先在仓库根目录运行 `python -m pip install -e ".[mcp]"`，再在 `../harnesses` 运行 `npm ci --ignore-scripts`。模型不随包下载。以下从本目录执行：

```powershell
npm run build
npm pack --ignore-scripts
```

构建只复用 `../harnesses` 编译产物与本仓库许可证，不复制上游插件源码。当前推荐使用[固定补丁宿主安装步骤](host/README.md)：独立宿主锁定修复依赖，安装和启动均通过完整路径启动器。以下直接调用全局 dsh 的命令仅说明上游 profile 操作，不代表该宿主已经清除依赖告警。

将生成的 `dsh-evernook-0.1.0-preview.25.tgz` 安装到独立 profile，设置仓库外的 DSH_HOME：

```powershell
$env:DSH_HOME = '<external synthetic preview directory>'
dsh --profile evernook --from-default-profile web --dump-config
dsh plugin --profile evernook add '<absolute path to tgz>'
```

只有新建的专用 profile 可使用 `--from-default-profile`；该命令会替换同名 profile，不能对已有用户 profile 运行。未配置时页面展示连接设置说明，不采集/修改记忆。

## 独立记忆进程

preview.23要求Core0.1.16的独立服务。先在独立后台进程启动，再配置或启动dsh。服务退出时请求明确失败；dsh退出仅关闭自己的客户端。长输入完整交给Core选择有边界的检索意图，缺少显式意图时会显示query_coverage_limited；已配置embedding和维护身份的服务可在宿主外继续小批量向量化。可选安装Core的vector extra（sqlite-vec0.1.9）加速现有向量的全历史精确候选，不改变原始库及向量格式；未安装时使用Python完整扫描。真实模型与规模门槛仍需单独通过。

```powershell
python -m evernook.service --config 'C:/external/memory/runtime.json' --maintenance-access 'C:/external/memory/mcp-access-background.json'
```

后台身份须单独配对，具备recall/maintain/accept；不传maintenance-access时服务仍接受普通操作。可选context/embedding配置由服务绑定；Web仍披露字符兼容，真实完整请求预算接线沿用#105。SDK可指定serviceRequired强制服务模式。旧客户端隔离与正式一次切换由#111验收，当前仅供隔离测试profile。

## 配置

在该 profile 的 `cordis.patch.yml` 增加下列覆盖，所有路径均为仓库外、已初始化且受 ACL 保护的绝对路径。普通身份必须 capture/recall；独立维护身份至少 recall/maintain/accept，不能与普通身份共用 client ID。插件只核验，不自动配对或提升权限。

```yaml
- id: dsh-evernook
  config:
    python: 'C:/external/venv/Scripts/python.exe'
    config: 'C:/external/memory/runtime.json'
    access: 'C:/external/memory/mcp-access-dsh.json'
    maintenanceAccess: 'C:/external/memory/mcp-access-management.json'
    entry: dsh
    streamMode: durable_text
```

```powershell
$env:DSH_TELEMETRY_MODE = 'DISABLED'
dsh --profile evernook --no-open
```

使用 dsh 打印的本次启动认证入口打开页面；不要公开该URL。预览只允许 loopback、同源页面，通过 dsh 已鉴权的 owner-only `/api` Fetch 路由，不新增监听端口；页面操作额外绑定进程内CSRF令牌。配置文件路径、维护凭证和主口令不传浏览器。卸载：`dsh plugin --profile evernook remove dsh-evernook`；它不删除外部记忆库。

安装、升级、卸载后须重启该专用 profile 并重新加载页面。目标dsh版本可能在热卸载后保留客户端缓存侧栏，本预览不声明无重启热更新/热卸载；路由撤销和工作进程退出不等于已经撤回宿主持有的旧页面副本。

## 数据与安全边界

- 页面正文以 React 文本渲染；来源与推断不是执行指令。记忆失败不转云端。
- 源更正追加替代证据，移出画像保留原话，来源抑制先展示已知影响；不承诺全库同义屏蔽。物理删除/恢复仍用可信 CLI。
- “兴趣与偏好”页同时展示已接纳的兴趣及偏好。未知发生时间保持未知，不把记录时间当发生时间。
- 跨处理器拒绝防重是确定性标签/来源身份匹配；新证据与主动重审单独处理。
- 普通记忆未做全库加密。dsh原始日志、其他插件副本及宿主聊天模型的数据出机不由EverNook控制；同账户恶意代码不在强隔离范围。
- 本包未发布到npm；安装步骤不等于真实模型、所有工具链、跨平台、长跑或物理断电验收。实际验证记录见 `docs/reports/`。
- 本包自己的MCP路径固定Zod4.6.7；未经修补的目标dsh仍固定旧Zod。新的[独立补丁宿主](host/README.md)固定Zod/Undici/MCP/fflate，2026-11-07实际宿主和插件profile生产审计均为0条，并验证61条依赖解析边；不代表任意全局安装或其他插件同样通过。完整工具链和日用仍受 [#92](https://github.com/destinyTaoTao/evernook/issues/92)、#81/#82门禁约束。

## 显式工具发布屏障

当前包包含可选的`toolPublication: true`，仅用于已应用发布补丁并通过回归的独立宿主。默认仍关闭；非补丁宿主启用时拒绝启动记忆连接。配置示例：

```yaml
- id: dsh-evernook
  config:
    # Retain all five explicit external connection paths/identity settings above.
    python: '<absolute interpreter path>'
    config: '<external runtime.json>'
    access: '<external capture access.json>'
    maintenanceAccess: '<external separate management access.json>'
    entry: dsh
    streamMode: buffered
    toolPublication: true
```

该组合对参数、最终结果、PTC调用展示与最终日志先取得完整保存回执，再允许发布；保存失败停止本轮。工具副作用可能已经发生，不能直接重试或假称回滚。失败/中止的原始会话保持待核对状态，可只读检查；没有自动补造结果或自动解封恢复入口。

上述`buffered`模式的模型响应仍整段保存后显示。`durable_text`继续只支持单文本块，启用工具屏障不会解除其工具/推理限制；[首批验证](../../docs/reports/dsh-tool-publication.md)列出实际通过范围。

preview.13另增显式`streamMode: durable_tools`，同样必须`toolPublication: true`。它让安全文本取得分块回执后连续显示；工具调用、完整参数和provider结束/重放元数据在真实结束并取得完整结构保存回执后发布，再复用最终结果屏障。顺序文本块和交错工具参数受支持；preview.18另支持有界推理块，推理等待真实结束和完整结构回执后展示，保持原始重放元数据，不混入答案文本流。推理在前时，后续内容会一起延迟；这不是推理的逐块可靠实时流。附件、重叠非工具块和未知结构仍明确拒绝。截断/取消/错误不伪造完成，已显示的安全前缀可通过原有流回取入口读取；部分文本可能因敏感尾部或前面的结构块暂缓显示。默认模式仍未改变。

此模式仍未通过真实聊天模型、工具解禁或正式日用验收，参见[混合流验证](../../docs/reports/dsh-mixed-stream.md)和#94/#81/#82。没有外部副作用回滚保证。

推理增量的实际SDK/Core及真实DeepSeek传输适配器的本地合成协议验证，见[#99记录](../../docs/reports/dsh-reasoning-stream.md)。本地协议通过不代表远端真实模型质量或所有部署兼容。

## 失败后的可信核对与只读续接

preview.19在已升级的独立宿主中提供“核对工具发布”：会话侧栏直接核对当前会话，独立页面可从本入口的时间线条目进入。只显示账本当前允许读取的调用参数、原始输入、结果回执和来源；完整回执、保存缺口和未确认分开显示。工具报告并不独立证明外部状态，副作用仍需核对。preview.19对已完成回执只读校验目标标记，不争抢正在讨论的目标写租约；缺失或不匹配的目标仍拒绝，不自动重建。

用户先预览，再明确确认“创建只读续接”。旧失败会话保持锁定，不补造工具结果；新会话只保存来源引用和核对元数据，创建动作不调用模型或工具。点击“打开只读会话”后，可主动讨论已保存证据。宿主禁止该会话的所有工具执行，换调用ID、重启、分叉或卸载EverNook均不解除限制；首版没有工具解禁入口。

核对授权与创建完成分开记入同一账本；创建持久化后才返回成功。来源版本变化、取消、过期或创建完成回执失败均明确处理，相同确认重试不会重跑旧操作。已删除的完成目标不会因重复确认自动重建。该能力要求本提交的Core及宿主只读限制补丁，旧宿主不提供入口；更新前须停止独立宿主，按[安装说明](host/README.md)校验和回归。

这是工具发布失败后的受限续接，未代替#82的运行库恢复或完整安装准入。边界与实测见[核对恢复记录](../../docs/reports/dsh-tool-recovery.md)。

机制参考：[dsh官方UI插件指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/preset/agent-preset/skills/cordis-plugin-development/references/ui-plugin.md)、[dsh-context](https://github.com/bowenliang123/dsh-context)。本插件沿宿主slot/locale/生命周期API独立实现，没有搬入其缓存、上下文压缩或记忆核心。
