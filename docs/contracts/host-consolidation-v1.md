# 宿主有界沉淀与补跑 v1

对应 [#77](https://github.com/destinyTaoTao/evernook/issues/77)。dsh/pi 显式配置 `consolidation: { access: <独立工作身份的绝对路径>, pollMs?: 5000 }` 后，启动、轮次完成、会话结束及空闲轮询调用同一 `ConsolidationScheduler`。不安装系统任务、不修改已安装宿主配置；默认不启用宿主后台工作。

普通宿主身份必须恰有 capture/recall；私有工作身份必须有 recall/maintain/accept，且 client_id 与访问文件均不同。后者只由可信适配器启动的固定 CLI 使用，不注册模型工具，也不进入 provider 消息。主题 provider 产生低置信度、有原文证据的 topic 草案；#78 额外接入[三类有限规则草案/受准入控制的发布](automatic-preferences-v1.md)，默认规则未准入。

前台开始先中止本宿主在途工作，再通过可信 MCP `evernook_v1_host_activity` 写入活动租约；该操作只要求 capture，recall 模式不提供。`session/active/ttl_seconds` 严格校验，租约属于当前 client_id，不能清除其他客户端活动。宿主为所有并行会话维护一个聚合租约，每10秒续期30秒；API允许1–120秒。活动同步失败时暂停本宿主后台触发，原文采集继续。核心在开始处理和 provider 返回时重新检查全库前台租约及暂停状态；现有事务提交不异步打断。

`scheduler-configure` 的 JSON 设置持久化在原账本；省略的字段保留原值。默认 max_sources_per_run=8、max_runs_per_trigger=1（各1–16），min_pending=1（1–1000000），idle_seconds=0（0–86400），max_trigger_seconds=10（0.1–30有限秒）。`paused` 对所有触发生效；manual 可绕过数量门槛，不能绕过暂停/前台。仅 idle 原因等待空闲延迟；startup/turn_end/session_end 可直接补跑。轮询范围100–60000毫秒，单宿主单个在途工作。

每次最多选择来源数×运行数条，沿用单来源1MiB、证据/草案与租约限制。Python provider 在调用前后协作检查时间预算，超时结果不发布、进度不前移；不能抢占任意阻塞的自定义 Python callback。宿主工作 CLI 有30秒外层时限和8MiB输出上限。Windows `evernook.worker` 将自身及后代加入带 KILL_ON_JOB_CLOSE 的 Job Object，使用真实进程句柄监控解释器启动器和逻辑 Node 宿主；取消、宿主退出及超时会回收工作进程树。它只终止自己拥有的 job，不终止父进程。启动保护无法建立时拒绝执行。依据：[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[扩展限制结构](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information)。

退出回调只作会话边界记录；可靠性依赖原始任务、持久进度与租约重领。全库调度租约串行认领窗口，资产提交和进度写入均在本事务内核对租约 token/期限；旧工作进程复活不能发布。中止后租约需等待预算向上取整+30秒，崩溃宿主的活动租约也需到期。dsh 使用 SDK finally 路径中的 turn/end 覆盖取消及异常；活动同步短暂失败后由空闲轮询重试。重启或后续轮询继续处理，不声称瞬间恢复或物理断电验收。session_marks 是回调次数，不是排队任务数或去重回执。

草案已落盘而进度未落盘时，先按原 run 的完整结果标记、全部草案 ID 集合及原来源版本恢复历史处理覆盖，再为新到消息组批。某个共同来源后来失效会使对应草案不可用，但不把已成功处理且自身版本仍有效的其他来源重新变成待办；不会凭缺失证据重建关联断言。队列逐草案核对当前证据，过滤失效正文，分页 offset 仍按底层记录前移，允许空页带 next_offset。彻底删除包含某来源的 run/结果证明前，在同一删除事务内仅保存有完整证明且不在删除范围的有效来源处理进度；不新增被删正文或目标引用副本。

可信 `scheduler-status` 返回 pending、oldest_pending_seconds、covered_messages、processed、lexical、foreground_sessions、skip_reasons、failure_reasons 和保存的设置。pending 只统计当前可检索且尚无沉淀进度的原始任务；covered_messages 只统计当前可检索且已有进度的消息，包含明确跳过；processed 为历史进度行数。lexical 独立报告索引覆盖，不随沉淀成功冒充追平。failure_reasons 是当前失败/过期运行的统计，重试成功后可消失；skip_reasons 是逐来源的持久跳过原因。整次触发的 paused/foreground/threshold/idle_wait/time_budget 由 tick.skipped 返回，宿主 snapshot 保存最后 tick/error。foreground_sessions 为活动租约行数，此适配器聚合多个会话为一行。旧 MCP status.consumer_running 仅描述该 MCP 进程内消费者，不代表另一个 CLI 工作进程。

验证边界：公开核心 API/真实 CLI、实际 Win32 工作进程与后代、真实 stdio MCP，以及锁定的 dsh/pi SDK 生命周期和 provider 回调。未覆盖桌面 UI、多插件组合、真实模型质量、物理睡眠/断电、CPU峰值和规模 p95。
