# DUI-3 — dsh 记忆管理预览插件

Ticket: [#91](https://github.com/destinyTaoTao/evernook/issues/91), part of [Spec #88](https://github.com/destinyTaoTao/evernook/issues/88)。日期：2026-11-07。正式日用未准入。

交付包：`dsh-evernook-0.1.0-preview.8.tgz`，SHA-256 `971384b095c44ff7ef384103065b6ee33efbb1a6bd38055bd263315ac84ea6a1`。最终包补全重启说明；与完成浏览器验收的v7比较，29个Node/client代码文件逐字节一致，v8另行通过隔离安装、重启和9项真实HTTP检查。包和原始截图/运行证据保存在仓库外或忽略的构建目录，不提交合成运行数据库、访问材料或认证URL。

## 交付

`plugins/dsh` 提供可打包的 host/client 插件；host复用已有SDK适配和Python管理入口，浏览器React由宿主模块表提供，没有第二个React根、HTTP监听器、云服务或权威记忆副本。一级页和会话侧栏共用画像/兴趣偏好/时间线/来源/历史/草案视图。支持接纳、拒绝、重审、更正、移出画像、来源抑制，先展示操作与版本/来源/影响，再由用户确认。取消预览持久化，已经提交的决定不因取消伪装回滚。

仅使用dsh已经鉴权的owner-only Fetch路由；额外检查原始Host/Origin、loopback、Fetch Metadata、JSON类型与进程内CSRF。dsh的Request URL是内部地址，不能代替原始Host。流式收集请求最多128KiB，最多4个管理子进程，固定CLI操作/路径，无shell。浏览器不持维护身份；模型仍无确认入口。slot注入、可选侧栏fiber和本组件资源有显式释放回归。

## 实际验证

环境：Windows、Python3.14.4、Node24.12.0，真实dsh CLI `0.2.1-rc.5` 的Web profile。独立DSH_HOME、profile、数据库、访问材料及合成证据均在仓库外；未修改日常profile。不是Windows原生Desktop应用验收。

| 检查 | 结果与范围 |
|---|---|
| 管理核心 | 6项通过：真实CLI、权限/跨端/操作绑定、过期/旧版本、依赖变化、故障事务回滚、幂等重放、原话分页、取消后重启仍不可确认 |
| 拒绝控制 | 4项通过：跨处理器/新tag、并行旧草案、主动重审/新证据、归档控制重放后发布新运行库；继承记录不作为新的用户拒绝列出 |
| 已有相关契约 | 来源控制8项、画像及当时新拒绝合计8项、沉淀17项、运行库恢复11项通过；这些是分别运行的相关子集 |
| 真实SDK | 5项通过：本轮回执排除、可靠文本与独立回放、Unicode大块、失败审查不能授权；合成provider，不调用真实聊天模型 |
| Host与client生命周期 | 2项通过：跨站/缺Origin/CSRF/超限/关闭请求，以及slot与可选fiber释放 |
| 类型与构建 | Python源码/新增测试/合成seed严格mypy44文件通过；TypeScript检查、插件构建、client语法检查通过 |
| 实际HTTP | 9项通过：未登录401、跨Origin403、owner bootstrap、缺失/伪造CSRF403、可信管理200、no-store、预览状态和伪造确认400 |
| 浏览器管理 | 真实页面接纳/拒绝草案、画像更正、移出画像、来源抑制及回读；Escape取消后数据库中有持久canceled记录，未修改画像 |
| 来源与时间 | 原话从2048继续到4096 code points；发生时间未知显示未提供，记录时间独立。会话侧栏按会话显示，画像仍跨已授权入口共享 |
| 不可信正文 | 含`<img ... onerror=...>`原话按文本显示，浏览器攻击图片元素数为0；没有把文字当HTML或执行指令 |
| 布局 | 真实1280×720截图；720×900下记忆区域clientWidth/scrollWidth均664，无横向溢出。未声称全部设备/DPI/主题验收 |
| 安装/卸载 | 本地tgz安装与重启、版本迭代后加载；卸载路由404、0个该合成配置的Python存活进程、外部数据库保留；卸载后重启再加载，入口与记忆DOM均消失 |

## 限制与残留

- 安装、升级、卸载须重启profile并重新加载页面。该dsh版本的热卸载曾保留缓存侧栏；补充自身资源释放不等于整个宿主支持无重启热卸载，不宣称该项通过。
- 插件自己的MCP依赖固定Zod4.6.7，harness的`npm audit --omit=dev`为0条；目标dsh peer固定旧Zod，插件依赖审计仍传播3条high，来自同一资源耗尽公告。未证明本页面存在可利用路径，也未修复宿主；跟踪 [#92](https://github.com/destinyTaoTao/evernook/issues/92)。
- 初次默认dsh启动曾执行宿主模型目录请求；后续UI验证禁用该provider和宿主遥测。EverNook没有聊天模型调用或云端fallback，但本轮不是整宿主物理断网验收。
- 页面可信点击是宿主责任，同源脚本、维护凭证持有者及同账户恶意进程不具备强隔离。普通库未做全库加密，宿主副本和已交给provider的上下文不能撤回。
- 独立事实争议裁决、人设/保险库、物理删除和恢复管理未加入此页面；已有可信入口保留。画像拒绝防重是确定性标签/来源身份，不是全库语义屏蔽。
- 默认预览为可靠单文本块；工具/reasoning/多块仍阻断。字符兼容预算明确标识，未把它当真实目标token预算验收。完整编码工具链、模型质量、规模、长跑与 #81/#82 继续阻断正式日用。
- 没有本轮全量Python/SDK最终通过声明，没有新性能保证或模型准入。

## 机制参考

沿 [dsh官方UI契约](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/preset/agent-preset/skills/cordis-plugin-development/references/ui-plugin.md) 与 [dsh-context](https://github.com/bowenliang123/dsh-context) 的host/client、slot、一级页和侧栏机制独立实现。画像派生页/来源追溯可继续参考 [Hindsight](https://github.com/vectorize-io/hindsight)，时间语义参考 [Graphiti](https://github.com/getzep/graphiti)，接入简化参考 [Mem0](https://github.com/mem0ai/mem0)，核心与宿主集成分离参考 [Cognee](https://github.com/topoteretes/cognee)。未安装评测这些记忆框架，不借用其benchmark或安全宣传。
