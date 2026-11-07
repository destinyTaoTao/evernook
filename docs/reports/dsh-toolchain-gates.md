# dsh 正常工具工作流：发布边界与下一批验收

日期：2026-11-08。实施跟踪 [#94](https://github.com/destinyTaoTao/evernook/issues/94)，关联 #88/#92/#81/#82。用户已授权独立宿主最小发布补丁；首批 [#95](https://github.com/destinyTaoTao/evernook/issues/95) 的缓冲工具屏障见[验证记录](dsh-tool-publication.md)，[#96](https://github.com/destinyTaoTao/evernook/issues/96) 补入显式[受限混合可靠流](dsh-mixed-stream.md)，[#97](https://github.com/destinyTaoTao/evernook/issues/97) 提供[可信核对与只读续接](dsh-tool-recovery.md)。旧会话不解封，首版没有工具解禁；完整日用工作流仍未交付。

后续 [#98](dsh-control-restore.md)补齐运行库发布的页面/恢复控制水位；[#99](dsh-reasoning-stream.md)补齐顺序有界推理的完整保存后展示及真实传输适配器本地协议验证。真实远端模型、附件、完整日用与物理系统门禁继续开放。

[#100](dsh-runtime-cutover.md)完成同机合成dsh运行库切换；[#102](dsh-tool-recovery.md)/[#103](windows-sqlite-path-race.md)/[#104](cancellable-sqlite-connection.md)修复回执写租约和Windows并发启动/检索争锁。随后[#101](dsh-installed-stability.md)在Core0.1.3/preview.19新空白profile完成1800秒/180回合、全部预定故障动作及后台收敛。它补齐合成真实安装长跑，不关闭#81/#82剩余质量、真实模型或物理系统门禁。

## 已确认的缺口

`node plugins/dsh/probes/tool-publication.mjs <private host directory>` 在真实dsh0.2.1-rc.5的Tools服务注册合成工具与结果观察者：观察者仍在等待保存时，`tools.execute`已经返回成功；观察者抛出存储异常时，执行仍返回成功。输出为 `observer_awaited:false`、`observer_failure_blocks_result:false`。这不能作为“先可靠保存，再显示/继续模型”的屏障。

已核对npm发布的0.2.2-rc.1：`lib/types/pipeline.js`的`finish`仍在final content转换后同步调用`notifyResult`，`notifyResult`对异步观察者仅附加catch，不等待。`tools/post-execute`虽然可等待，但发生在工具自有最终内容转换之前，不能保存后就声称保存的是最终结果。不得用改注册顺序、同步阻塞子进程或后台补写规避这个缺口。

现有EverNook默认`durable_text`继续阻断工具/reasoning/多块；buffered仍是整段兼容路径，只有显式启用已补丁宿主的`toolPublication`才增加原生工具发布前保存。真实SDK的工具事件事后采集、精确回源和重复rescan本身不等于发布前持久化通过。

## 已授权并实现的最小宿主扩展

在独立固定宿主中，对**确切版本和文件哈希**应用补丁；不创建长期上游分支，不修改全局安装。已增加单独的可等待最终发布钩子，位置为所有工具/策略/最终内容转换和lossless JSON验证之后、`tools/result`通知及执行结果返回之前。普通观察者语义保持不变。补丁若不匹配上游文件、无法应用或回归失败，安装必须失败，不能静默忽略。PTC 另外覆盖调用展示和最终日志，避免只保存内部结果却发布不同内容。

钩子输入为冻结的执行身份和最终结果；结果可含错误、拒绝、取消以及附加上下文，不能只覆盖成功调用。EverNook通过独立来源身份可靠保存，再允许宿主发布。存储失败必须阻止该原结果发布；不能让宿主的常规catch把保存异常转换成另一份未保存结果继续发布。工具副作用可能已经发生，必须保留“已执行但未确认保存”的诊断状态并停止本轮，不能自动重新执行或暗示副作用已回滚。

补丁把同步的最终finish及全部调度分支改为可等待；这是宿主接口扩展和维护负担，不能称为纯插件完成。#96 已补规范文本/工具块的可靠发布和敏感/超限处理；真实模型完整兼容、核对解封与恢复联动及冻结准入仍不能由钩子成功代替。

## 小批次验收顺序

1. 宿主钩子合约：挂起保存时任何最终结果均未发布；保存失败阻断；最终内容转换后逐字一致；并行调用仍按模型顺序；拒绝/取消/嵌套工具均经过屏障。
2. 插件接入：工具调用身份与输入回执关联、只保存一次；文本与工具块、Unicode与超限；失败状态不触发重复外部副作用。每组固定少量合成场景和时间上限。
3. 实际独立安装：合成provider驱动文件读取、临时工作目录中的写入与shell最小命令；权限拒绝、取消、宿主退出后的原话/结果回取与恢复；确认原生二进制可用。
4. 正式日用：维持#81冻结质量/规模门槛和#82实际离线/睡眠/重启/长跑界限。不得通过调整阈值或以UI/审计成功代替准入。

实际OS睡眠/重启影响整台机器，最后需独立安排可观察窗口；本批没有执行这些系统动作，也未切换用户日常profile。
