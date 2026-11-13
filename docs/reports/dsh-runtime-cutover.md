# 独立安装的 dsh 运行库切换与只读恢复

2026-11-08，实施 [#100](https://github.com/destinyTaoTao/evernook/issues/100)，接续 #98/#99，关联 #82/#94。使用已明确隔离的合成 profile、仓库外运行数据和固定补丁宿主；没有切换日常 profile、调用真实模型或修改全局 dsh。

## 产物与安装身份

用 uv 构建当前 Core wheel，在新的独立 Python3.14.4 虚拟环境安装 wheel 与 MCP 依赖；没有 editable 或 system-site-packages。45个包内Python/Node文件逐一比较源码、wheel成员与实际site-packages，全部字节一致。版本仍为0.1.1，不能和主线历史同版本产物混同；本次源代码基线为 `485cf76`，后续本票仅增加验证脚本与记录。

| 实际产物 | SHA-256 |
|---|---|
| evernook-0.1.1-py3-none-any.whl | `f4cdf378f5e38bef8587a384eb71946b1f81fd4fb0976ba67b22ce0d44743af8` |
| dsh-evernook-0.1.0-preview.18.tgz | `de28861aa6b6c0175d6bcda05a5c1e1ef43f95f194133b921a04ba998ab439a6` |

目标为 Windows本机、Node24.12.0、dsh0.2.1-rc.5。冻结宿主的61条实际依赖解析边通过，实际插件profile生产审计0条；宿主补丁和插件产品代码没有在本批改变。

## 实际步骤与结果

1. 新增合成重要原话、来源更正/抑制/删除控制、取消预览及只完成第一块的可靠流。在同一测试profile用独立安装的Python连接原库，核对已有只读恢复确认仍幂等。
2. 停止测试宿主，确认该夹具的Python工作进程为0。按既有协议导出当前快照、捕获可信检查点、重放控制并发布独立运行库；没有自动复制旧访问令牌。
3. 新库拒绝旧owner/capture/maintenance三种身份。显式重新配对同名capture与维护调用端、生成新令牌，分别只授capture/recall与recall/maintain/accept；重配后旧采集/维护令牌仍拒绝。
4. 只在该测试profile修改config/access/maintenanceAccess路径并重启，Python保持新独立安装。实际MCP工作进程参数均指向新库。原生临时文件read正常，2次合成请求、1份结果、0错误/阻断，推理与完整调用/结果回执可回取。
5. 原已完成的恢复确认返回同一个持久只读目标；恢复动作无模型/工具调用，原文件内容/mtime和旧失败会话指纹不变。随后在真实宿主resume该只读会话：合成讨论的2次主模型请求均带恢复证据；新的写文件调用返回 `EVERNOOK_RECOVERY_READ_ONLY`，工具体执行计数0，目标文件不存在。之后重复确认仍是同一目标。
6. 通过真实owner页面API逐字读取原话、拒绝已更正/抑制来源和已取消确认，读到仅在新库发生的原生会话时间线，核对只读目标仍存在。页面读取与核对没有触发额外模型/工具执行。9项HTTP鉴权/跨站/大小边界及真实设置保存通过。
7. 新库Core入口逐字读回重要交代、当前更正值；被删试样不可读，重复删除请求返回原完成回执；取消不能重新激活。旧可靠流从下一index继续并完成，全文精确为原已确认前缀加新块。整个过程中，旧源库目录下所有文件哈希保持冻结值。
8. 最终卸载产品与合成探针，重启后路由404；相关Python进程0，关闭测试端口。原库、新库、产物和合成证据均保留在仓库外，没有删除或降级覆盖。

## 可重复验证入口

`plugins/dsh/probes/runtime-cutover.py` 提供 `identity/prepare/publish/verify --root <existing external synthetic root> --checkout <checkout>`。仅接受既有 `evernook-publish94-*` 夹具，约定源数据位于 `web-synthetic`、新产物与目标位于 `cutover-stage`；必须用新独立安装的解释器调用。`prepare/publish`完成后拒绝重入，避免再次打开冻结源库；`verify`可重复回查，并只在新库续写未完成的合成流。实际验证这两个重入拒绝后，源库哈希仍未变化。脚本不是通用用户切库工具。

`runtime-cutover-http.mjs <private launch log> <fixture.json> <native read report> <recovery report> <readonly report> <external result>` 核对实际owner通道及只读拒绝。扩展的原生探针仅从受信合成计划resume带持久只读标记的保留ID；模型不能传入任意恢复目标。

`native-tools-http.mjs <private log> <existing report> verify-saved <paths.json>`仅重新核对已发生的原生结果和账本，不再次发起工具请求。只读宿主故意跳过pre-execute钩子，其被拒调用以最终屏障前已采集的原生参数及完整capture回执核对；拒绝错误元数据来自原生事件的`data.error`。验收脚本最初假定这些字段位置/调用路径与普通工具相同，已按真实宿主证据修正，随后用保存报告复核，未重发讨论或写文件请求。

新增Python探针strict mypy、三个Node探针语法和差异检查通过；全部业务验证使用实际wheel/Profile/MCP路径。本批没有运行全量Core/SDK套件，也没有重复浏览器UI全流程。

## 仍未完成

这证明同一机器、同一JSONL测试profile切换到新的独立运行库可用；不代表跨机器迁移、实际用户库切换、断电、物理睡眠/重启、整机断网或新的长跑验收。dsh JSONL文件仍保留在原测试profile；恢复库也不自动同步之后旧库的新写入。实际操作仍须停止旧入口并统一切到选定的权威库。#81模型质量/规模以及#82剩余物理系统/长期门禁保持，日常模型未启用。
