# Web预算模式披露与计数接线原型

2026-11-08，接续[#105](https://github.com/destinyTaoTao/evernook/issues/105)。本批preview.21只完成实际模式披露及独立接线原型，**没有实施Web目标预算主体接线，也没有准入任何provider计数器**。

## 已实现的状态披露

owner bootstrap新增`context_mode`，从实际安装选项和连接状态派生：未配置为`unconfigured`；配置后按SDK实际预算选项区分`legacy_character_cap`与`target_tokenizer`。当前Web尚未转交目标预算选项，因此实际可验证的是前两种状态，不能通过请求正文指定目标模式。

独立页面/侧栏按服务端状态显示中英文预算模式；字符兼容明确说明不是真实token预算，缺失/未知状态不按目标模式展示。刷新或切换会话时先清理旧状态，随后采用bootstrap/当前会话返回的状态。全局`daily_admitted=false`保持。这里只验证源码、构建、生命周期与实际API，没有声称完成浏览器渲染验收。

新增HTTP回归先以`undefined != unconfigured`失败，修改后HTTP/client生命周期2项通过（303.332ms）；涵盖未配置状态、非准入状态及伪造目标模式请求。TypeScript typecheck、client语法与构建通过。

## 实际固定宿主安装

使用已有只读固定补丁dsh0.2.1-rc.5，在新的`evernook-budget-status`专用profile安装preview.21，Core仍为独立非editable0.1.5。不调用聊天模型，没有更改旧profile、日用配置或原生二进制。

- 未配置：bootstrap返回`unconfigured/configured=false/daily_admitted=false`，管理读取409。
- 配置后：返回`legacy_character_cap/configured=true/daily_admitted=false`，管理读取200。
- 伪造模式：不带CSRF返回403；已配置且带有效CSRF仍返回400，不能将请求正文变成目标预算设置。
- 既有owner HTTP9项、实际61条依赖解析边通过；实际profile生产审计0条。依赖图未新增第三方成员。
- 插件37成员逐字节匹配源码、tgz和实际安装；tgz SHA-256为`e886d1d2257182ab7961f206a66746fa5ad89f5d4d8d948152586ebc76ae1b28`。
- 卸载并重启后管理路由404；专用进程树已停止，相关Node/Python进程0，外部运行库与证据保留。

独立安装原始证据位于仓库外`Documents/Codex/2026-11-08/evernook-105-budget-status-01`。认证URL、运行库、身份和日志不提交。

## 已核对的上游边界

本机固定`dsh-token-meter0.2.1-rc.5`的公开实现采用`CHARS_PER_TOKEN=4`及结构开销启发式；不能把它当真实目标分词器。固定`dsh-llm0.2.1-rc.5`的adapter/PreparedLlmCall接口提供模型元数据和绑定派发，没有完整同步目标token计数入口。该判断限定于这些固定产物，不推广到所有上游版本或远端模型。

公开产物核对SHA-256：token-meter `lib/index.js`为`b76a27f84d1f733c4a760e36fcc0491f807b32d51055703feb0fed1643cc2e05`；llm `lib/types/index.d.ts`为`895a2d357b3da2d2ec18eada1271274d43169acaa6591d57bb6e84878e1b88fe`。

## 已确认路线（2026-11-08）

用户选择等待dsh原生计数接口，不增加自维护的provider计数服务。当前Web保留明确披露的`legacy_character_cap`，`daily_admitted=false`；#105保持开放，改为等待上游能力，不再等待本项路线答复。既有显式SDK目标预算能力保留。

后续须先核对原生接口能否按实际provider/model覆盖完整聊天模板、历史、工具声明与输出预留，并验证计数身份、路由变化、取消和来源失效边界，再决定最小接线。原生接口存在、字符启发式或模式标签本身均不能作为准入证据；本次不升级宿主、不注册生产计数服务，也不启用日用profile。

## 未采用的计数服务提案与原型证据

此前建议由实际provider的受信插件提供一个计数服务，Web只接收provider/model、外部context设置和预算等纯数据；不新增任意函数/模块路径加载入口。复用SDK的完整请求计数与Core资产校验，缺失、不匹配或服务替换时拒绝，替换后重启profile。该路线需要逐provider维护并验证framing适配，本次未采用。

真实Cordis4.0.5的仓库外小原型验证4项：普通服务对象身份稳定、模型不匹配时不调用计数器、注销后已有guard拒绝、同名对象替换后已有guard拒绝。使用现有`ctx.get/provide`，没有声明可因计数服务注销而一并卸载guard的依赖。原型计数函数故意只是合成字符长度；它仅验证服务生命周期，不是tokenizer、provider framing、实际Web派发或质量准入证据。原型位于`evernook-105-counter-seam-01`，生产未注册此服务。

主体接线仍须核对实际provider完整聊天模板/工具声明/预留、计数资产身份、provider路由变化、取消/来源失效与只读恢复边界。不能仅凭服务对象存在便宣称计数正确。#105所有完整验收项仍未通过，#81/#82/#94门禁保持；本批不替代真实模型、p95/规模、物理OS、旧组合长跑或浏览器复验，待确认ORT原生补丁也未实施。
