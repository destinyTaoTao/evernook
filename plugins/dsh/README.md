# EverNook · 忆栖 — dsh 记忆管理预览

一级记忆页面与会话右侧栏，复用同一 Python/SQLite 证据账本。提供画像/兴趣/时间线、来源分页、版本历史、更正、移出画像、来源抑制与草案接纳/拒绝/重审。页面变更先预览，用户确认后保存，模型无管理凭证。

**预览目标：Node 24、dsh 0.2.1-rc.5、Windows本机。** 未声明其他版本兼容或正式日用准入；文本可靠流模式拒绝工具/reasoning/多文本块，buffered 模式整段保存后显示。原生工具屏障需显式启用下述独立宿主组合；#94/#81/#82继续适用。

## 构建与隔离安装

Python 3.14 环境须安装本次提交的 Core（包含 `manage` CLI），例如先在仓库根目录运行 `python -m pip install -e ".[mcp]"`，再在 `../harnesses` 运行 `npm ci --ignore-scripts`。模型不随包下载。以下从本目录执行：

```powershell
npm run build
npm pack --ignore-scripts
```

构建只复用 `../harnesses` 编译产物与本仓库许可证，不复制上游插件源码。当前推荐使用[固定补丁宿主安装步骤](host/README.md)：独立宿主锁定修复依赖，安装和启动均通过完整路径启动器。以下直接调用全局 dsh 的命令仅说明上游 profile 操作，不代表该宿主已经清除依赖告警。

将生成的 `dsh-evernook-0.1.0-preview.11.tgz` 安装到独立 profile，设置仓库外的 DSH_HOME：

```powershell
$env:DSH_HOME = '<external synthetic preview directory>'
dsh --profile evernook --from-default-profile web --dump-config
dsh plugin --profile evernook add '<absolute path to tgz>'
```

只有新建的专用 profile 可使用 `--from-default-profile`；该命令会替换同名 profile，不能对已有用户 profile 运行。未配置时页面展示连接设置说明，不采集/修改记忆。

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

preview.11增加可选的`toolPublication: true`，仅用于已应用发布补丁并通过回归的独立宿主。默认仍关闭；非补丁宿主启用时拒绝启动记忆连接。配置示例：

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

此模式的模型响应仍整段保存后显示。`durable_text`继续只支持单文本块，启用工具屏障不会解除其工具/推理限制。文本与工具混合可靠流、完整恢复及正式日用仍按#94/#81/#82推进；[本批验证](../../docs/reports/dsh-tool-publication.md)列出实际通过范围。

机制参考：[dsh官方UI插件指南](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/preset/agent-preset/skills/cordis-plugin-development/references/ui-plugin.md)、[dsh-context](https://github.com/bowenliang123/dsh-context)。本插件沿宿主slot/locale/生命周期API独立实现，没有搬入其缓存、上下文压缩或记忆核心。
