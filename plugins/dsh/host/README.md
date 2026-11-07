# 固定补丁宿主（Windows Web 受限预览）

对应 [#92](https://github.com/destinyTaoTao/evernook/issues/92)。这是独立目录中的上游 npm 宿主与固定依赖组合，不替换全局 dsh，不改上游源码。目标 Node >=24.11 <25、dsh 0.2.1-rc.5、Cordis 4.0.5、schemastery 3.18.5；插件仍为 0.1.0-preview.8。新版 0.2.2-rc.1 仍固定 Zod 4.4.3，且有 API 迁移，本批没有把升级当成依赖修复。

| 固定覆盖 | 原因 |
|---|---|
| zod 4.6.7 | 修复 constrained collections 资源耗尽告警 |
| undici 8.12.4 | 修复本次审计中的网络客户端告警 |
| @modelcontextprotocol/client 2.4.0 | 修复跨源重定向转发请求材料告警 |
| fflate 0.8.3 | 修复畸形 ZIP64 解压死循环告警 |

锁文件包含完整解析版本与 integrity。`provision` 只接受**不存在、父目录已存在、位于 checkout 外**的绝对路径；`npm ci --ignore-scripts` 有 180 秒安装界限，审计有 60 秒界限，失败保留外部诊断目录并以非零退出，不自动降级或运行 audit fix。版本覆盖只用于本清单，不应复制到任意用户依赖树。

从 `plugins/dsh` 执行，先按插件 README 构建 tgz：

```powershell
npm --prefix ./host run provision -- '<new absolute external host directory>'
$verifiedHost = '<external host directory>'
$env:DSH_HOME = '<existing dedicated external DSH_HOME directory>'
$env:DSH_TELEMETRY_MODE = 'DISABLED'
node ./host/launch.mjs $verifiedHost --profile evernook --from-default-profile web --dump-config
node ./host/launch.mjs $verifiedHost plugin --profile evernook add '<absolute preview.8 tgz path>'
node ./host/verify.mjs $verifiedHost "$env:DSH_HOME/profiles/evernook"
pnpm --dir "$env:DSH_HOME/profiles/evernook" audit --prod
# Add the explicit external EverNook paths to cordis.patch.yml, as in ../README.md.
node ./host/launch.mjs $verifiedHost --profile evernook --no-open
```

`--from-default-profile` 仅用于全新专用 profile，会替换同名配置。`launch.mjs` 要求显式 DSH_HOME，使用 `realpathSync.native` 统一宿主和 DSH_HOME 的物理路径；Windows 8.3 短路径可能使 dsh 启动模块加载两份，导致首次确认/设置保存报 `profile reload requires the root Include entry`。不要绕过启动器直接用短路径运行宿主 bin。

`verify` 从所有已安装依赖消费者实际解析四项覆盖版本，不能只查看顶层 Zod；额外检查 profile 中插件的 Zod/MCP 及 `autoInstallPeers: false`。宿主锁文件审计和实际 profile 的 pnpm 审计必须分别通过。增加/升级其他插件后必须重新审计与验收；联网审计反映当时的公告，不保证没有未知漏洞。

此安装跳过第三方 lifecycle 脚本，已验收的是 Web 记忆预览。原生 shell/终端二进制、完整工具工作流和可靠结果发布另行验收，不能据此启用日用。卸载仍通过同一启动器：

```powershell
node ./host/launch.mjs $verifiedHost plugin --profile evernook remove dsh-evernook
```

安装、升级、卸载后重启 profile 并重新加载页面。外部证据库保留；不要对现有宿主目录运行原地覆盖安装。复现路径、验证结果及未通过项见 [本批记录](../../../docs/reports/dsh-host-dependencies.md)。
