# 固定dsh安装组合的稳定性验证

2026-11-08，跟踪[#101](https://github.com/destinyTaoTao/evernook/issues/101)，关联#82/#94。仅使用仓库外合成数据、空白独立DSH_HOME/运行库和固定补丁宿主，不切换日常profile，不调用真实远端模型。

## 冻结范围

沿用既有1800秒观察、10秒前台间隔、250ms进程树采样、2147483648字节采样PrivateBytes上限、10737418240字节实验逻辑磁盘上限。新`stability-policy.json`与首个失败pilot冻结policy逐字段比较，除Core/插件版本身份外完全一致。Node24.12.0、Python3.14.4、dsh0.2.1-rc.5。目标为Core0.1.3/preview.19。

| 产物 | SHA-256 |
|---|---|
| evernook-0.1.3-py3-none-any.whl | `2b7b62f0972deaf4b67cfa8b77609306d2f9370c1112c3298707b3473dd60f85` |
| dsh-evernook-0.1.0-preview.19.tgz | `7e8ce8f78e407b8dfe0f319650868e9c483aabf6d097bfdf2c330c2b989b3618` |

独立非editable Core安装45文件逐一核对源码/wheel/site-packages；实际profile61条依赖解析边通过，生产审计0条。预检保留网络拒绝3项回归，包括MCP传输不传递自定义环境变量时的独立Python日志选择。

## 保留的pilot

| 轮次 | 组合 | 结果 |
|---|---|---|
| 01 | Core0.1.1/preview.18 | 6前台回合后恢复确认HTTP400，仅有通用错误记录；不能推定精确根因 |
| 02 | Core0.1.1/preview.18 | 6回合后`unsafe_runtime_path`；后续真实SQLite生命周期诊断定位扩展盘符与可选日志ACL竞争，见[#103](windows-sqlite-path-race.md) |
| 03 | Core0.1.2/preview.19 | 1回合后`storage_busy` |
| 04/05 | 同上，临时诊断 | `storage_busy`；工作线程定位在检索连接配置，见[#104](cancellable-sqlite-connection.md)；诊断仅记错误身份/栈位置，已移除 |
| 06 | Core0.1.3/preview.19 | 6回合与全部故障/恢复动作通过，约69.97秒观察；仍不是正式窗口 |

另有独立SDK红绿回归定位已完成恢复回执争抢目标写租约，已在[#102](dsh-tool-recovery.md)修复；没有把它当作上述HTTP400或路径错误的已证实根因。失败pilot原报告、policy哈希及合成证据均保留，未被后续结果覆盖。

## 探针行为

`plugins/dsh/probes/stability.mjs <private setup.json> <fresh prepared fixture> [pilot seconds]`连接实际安装的client，使用真实dsh原生工具和实际安装wheel。正式运行不传pilot参数；资源包装器为`probes/admission/monitor.py --max-seconds 2100 -- node ...`。前台按绝对10秒节拍调度，重启故障造成的迟到保留在时间戳，不通过累计漂移少跑回合。

每轮交替检查安全前缀与有界推理的原生read，按真实调用seq和callId回取完整结果，逐字比较工具内容，核对独立文本流回放。后台运行实际词法增量与导航tick。固定时点触发取消、权限拒绝、拥有的宿主树强停/重启和MCP reader重连。只在准备阶段执行一次合成写入并故意阻断发布；创建只读恢复目标，重复确认核对原文件内容/mtime及模型/工具计数不变；续接讨论中的新写请求必须被宿主拒绝、工具体零执行、文件不存在。

Node preload和独立环境的Python sitecustomize仅允许应用TCP/DNS/fetch的loopback路径；不改整机网络，不声称是OS防火墙或恶意代码沙箱。网络尝试只记运行时/类型/PID/时间，不记主机、参数或凭证。日志、cookie/CSRF、访问材料与原始报告留在仓库外。策略/driver/preload在启动时复制冻结，运行期间修改driver/preload会拒绝继续；外部setup和产物身份另保留。

磁盘按独立宿主与整个实验根目录逻辑文件累计，跳过符号链接，包含旧pilot和多个独立环境，较单次运行保守。资源采样覆盖拥有的活进程树；短命子进程、采样间峰值、父进程退出后孤儿及查询失败可能遗漏，不保证硬峰值。

复现setup需显式提供`synthetic: true`、既有私有实验`root`（名称以`evernook-publish94-`开头）、已核对的`host`、专用`home/profile`（profile以`evernook-stability`开头）、loopback测试`port`、独立安装版`python`、`checkout`、受信合成`plan`和本票`policy`的绝对路径。fixture必须先用该Python执行`probes/installed/fixture.py prepare --output <fresh child>`；仅该新运行库另外配对独立`recall/maintain/accept`管理身份，将路径填入专用profile配置。产品包和`native-tools-plugin.mjs`合成bundle须分别安装，合成bundle不属于产品包。Python guard只复制到该独立环境的sitecustomize，不能安装到用户通用Python。setup不含令牌正文，运行时从ACL检查的独立文件读取。

每次运行使用新fixture、资源目录和私有启动日志；不要在旧failed fixture上重发恢复写入或用旧报告覆盖新结果。失败后先保留原回执和停止相关进程，再针对原因做独立修复与新安装。应用网络拒绝属于实验约束；若需要整机断网或物理睡眠/重启验收，应另安排可观察窗口。

## 正式结果

formal-01未通过：43回合、423.5秒观察后，磁盘探针枚举到的`memory.sqlite3-wal`并发消失，`lstat`抛ENOENT；此前业务检查无错误、网络0。整体447.08秒，1786样本，查询失败0，采样峰值782884864字节。记录保留，相关进程和端口已清理，不能计为完整窗口。

磁盘探针改为仅跳过已从命名树消失的条目，权限及其他I/O错误继续失败；真实枚举/删除竞争及EACCES两项回归通过。命名树逻辑大小不是物理磁盘分配或原子快照，不能声称严格峰值。此修复不改变资源门槛或产品产物。

formal-02未通过：50回合后，第51次混合流的前缀回放核对漏翻页。原生前缀已实时显示，工具结果完整保存；探针仅读取100条marker的第一页，过滤后未找到当前session便误报。停止后不重发工具，只读按`next_offset`回查：第一页无该session/next100，第二页找到2个complete流，前缀逐字一致。生产API原本返回分页游标；此处仅修正探针分页并检查游标前进。503.2秒观察、整体526.92秒、2105样本/查询失败2、采样峰值777097216字节，网络0；记录与只读回查证明保留。

formal-03在新的空白DSH_HOME/运行库中完成：

| 检查 | 实测 |
|---|---|
| 观察/前台 | 1800.007883秒，180回合；每回合完整原生结果回读及对应前缀/推理检查通过 |
| 预定故障 | 取消、权限拒绝、宿主树强停/重启、MCP reader重连、只读续接新写请求拒绝全部通过 |
| 副作用 | 重复恢复确认无新增模型/工具执行，原文件内容/mtime不变；只读请求工具体0、目标文件不存在 |
| 错误/网络 | 业务错误0；应用外网尝试0 |
| 内存采样 | 7295次；最大进程树PrivateBytes782991360字节（约747MiB），低于2GiB |
| 逻辑磁盘 | 907062716字节（约865MiB），低于10GiB，含旧pilot与多个安装环境 |
| 后台收敛 | 901个维护tick；最终词法6982/6982、pending0；导航游标6982与消息头一致，queued/running/failed均0 |
| 一致性 | 6982条账本消息；SQLite quick_check为ok、foreign_key_check空；独立安装45文件再次与源码/wheel一致 |
| 结束清理 | 包装器0退出、整体1826.06秒；相关Python/Node进程0，四个测试端口监听0；保留所有库与证据 |

7次进程查询失败如实保留在资源报告，采样与命名树统计的上述非原子边界仍适用。`acceptance.json`逐项核对完整窗口、回合、动作、资源阈值、冻结脚本/策略哈希、后台收敛和数据库一致性；不把采样峰值说成硬上限保证。没有修改产品产物或资源阈值来取得通过。

本票完成固定安装组合的合成原生工具长跑；#81模型质量/规模、真实远端模型完整兼容、附件、用户库/跨机器切换、物理整机断网/睡眠/重启与其他平台仍未验收，#82/#94继续开放，日常profile未启用。
