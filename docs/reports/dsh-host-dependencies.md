# dsh 独立补丁宿主依赖验证

日期：2026-11-07。Ticket [#92](https://github.com/destinyTaoTao/evernook/issues/92)，关联 #88/#81/#82。仅受限预览，正式日用仍未准入。

## 结论与交付

最新npm的dsh为0.2.2-rc.1，已发布新版的dsh-llm仍精确依赖Zod4.4.3，单纯升级不修复这项风险；新版还涉及工具/投影API迁移。本批按已确认方向，保留已测0.2.1-rc.5，在仓库外独立安装固定补丁依赖，未更改全局dsh、日常profile、用户数据或上游源码。

`plugins/dsh/host`提交完整npm锁文件、限时且拒绝覆盖既有目录的安装器、实际消费者解析验证与显式DSH_HOME启动器。固定Zod4.6.7、Undici8.12.4、MCP client2.4.0、fflate0.8.3。只覆盖Zod的首个候选仍有63条moderate与1条high传播告警；去重后根因是另三项依赖，补齐后实际宿主与插件profile生产审计均为0条。它是当时公告库的结果，不是无漏洞或任意插件组合的保证。

安装有180秒界限，审计有60秒界限，失败保留诊断目录且非零退出。跳过第三方lifecycle脚本，Web预览已验证；原生工具二进制不因此自动视为可用。增加/升级插件需要重新审计与兼容性验收。插件包未变化，仍为`dsh-evernook@0.1.0-preview.8`。

## Windows路径回归：复现、归因与修复

候选首先出现首次欢迎确认保存失败，实际API返回`settings/rejected: dsh: profile reload requires the root Include entry`。新增`probes/host-settings.mjs`对真实owner接口读取revision、保存欢迎确认并回读；失败退出1，不输出认证材料。原版同版宿主成功，候选失败；停用EverNook后仍失败，shallow安装布局也失败。

临时外部定点日志显示同一`dsh-app-boot/lib/index.js`被分别用`DENGTA~1`与`dengtaotao`两种URL加载，各自持有独立的`bootstrapIncludes` WeakMap。Node的普通`realpathSync`仍保留8.3路径，`realpathSync.native`展开为完整路径。统一宿主入口与DSH_HOME后，原有API失败检查转绿，真实欢迎弹窗消失，后续设置保存再次成功。外部调试修改已还原至原始文件哈希，没有提交或保留上游补丁。

启动器将两条路径均物理规范化；实际以短路径参数调用新启动器、完整重新安装的宿主通过同样的检查。不能把此回归归咎于Zod，也不能靠跳过欢迎弹窗掩盖。

## 实际验证范围

环境：Windows、Node24.12.0、Python3.14.4；独立仓库外的宿主、DSH_HOME、profile和合成记忆库。没有使用私人对话或真实provider。默认宿主遥测和DeepSeek provider禁用；不是物理断网验收。

| 批次 | 实际结果 |
|---|---|
| 可复现安装 | 从提交清单/锁文件重新npm ci，565个包；安装和审计成功。候选与重新安装的所有非根lock节点版本/integrity差异0 |
| 依赖实际解析 | 从每个已安装消费者检查61条相关依赖边；四项修复版本一致。profile插件Zod/MCP及关闭自动peer安装通过 |
| 生产审计 | 实际独立宿主npm audit和已安装profile的pnpm audit均0条；原始Zod-only候选失败证据保留 |
| 宿主设置 | 真实settings读→版本绑定保存→回读成功；修复前红、规范化后绿；新启动器复验通过 |
| 真实HTTP | 最终启动器宿主9项通过：未登录、跨源、CSRF、owner管理、no-store、未准入标识、伪造确认拒绝 |
| SDK小批次A | 5项通过，约25秒：两种预算的输入回执隔离、独立可靠文本回放、Unicode/大块分片、失败维护审查 |
| SDK小批次B | 3项通过，约15秒：取消后新适配器/重复rescan、敏感分片与工具拒绝、原生文件/工具事件精确保存及输入关联 |
| SDK组合真实性 | 外部fixture仅复制本仓库适配源码和测试，dsh/Cordis/MCP解析到实际补丁宿主，tokenizers解析到现有固定0.2.0；不是用旧rc.3节点冒充rc.5测试 |
| 浏览器 | 实际启动器宿主主页面、原话来源、草案预览→确认接纳→兴趣页回读；会话右侧入口可打开。本批未重跑此前完整侧栏数据操作、更正/抑制/布局场景 |
| 工具API缺口 | 实际Tools服务证明异步结果观察者不被等待，保存异常不阻断成功结果；准入保持false，详见[下一批边界](dsh-toolchain-gates.md) |
| 静态检查 | 新Node脚本语法检查通过；本批未更改Python/SDK/客户端代码，没有新增全量套件通过声明 |
| 安装/解析拒绝边界 | 已有目录拒绝、checkout内目标拒绝且未创建、既有lock哈希不变；外部诊断副本中将实际Zod清单改成旧版本后验证失败，恢复原字节后通过 |

通过同一启动器卸载后，初次立即探测仍为200，宿主完成异步卸载后复验路由404；合成配置对应Python存活进程0，外部数据库保留。重装并重启后再次验证，最后停机卸载并重启：路由404，浏览器重新认证后无记忆导航、记忆DOM数量0；结束时本批三个测试端口均无监听。不能把卸载CLI返回当作路由已经撤销；客户端仍要求重启和重新加载。

完整原生shell/终端、真实模型、目标token预算联动、质量/规模、物理离线/睡眠/重启和长期验收仍未完成。#92不因依赖告警归零而提前关闭其工具链验收要求，#81/#82保持门禁。

## 公告与发布来源

- [Zod GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5)
- [MCP client重定向公告](https://github.com/advisories/GHSA-6prh-2h8m-c8cw)
- [fflate ZIP64公告](https://github.com/advisories/GHSA-px8p-9vwx-vf98)
- [Undici公告示例](https://github.com/advisories/GHSA-xjj6-f33q-929c)，此次同一安装节点另有7条公告
- [dsh 0.2.2-rc.1官方发布](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1)

版本和完整依赖来自当日npm发布清单/实际lock与audit；安全公告用于界定依赖风险，未声称EverNook页面存在已复现的利用路径。
