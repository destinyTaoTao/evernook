# pi 有来源任务续接 v1

对应 [#80](https://github.com/destinyTaoTao/evernook/issues/80)，复用[核心续接契约](work-continuation-v1.md)。固定 pi-coding-agent/pi-ai 1.4.0，只有显式 contextConfig + memoryBudget 路径声明本契约。普通模型身份仍为 capture/recall，未增加事实接纳或维护权限。

## 原生生命周期与分支

session_before_compact 使用 SDK 公开 compact 函数和原生 preparation，返回 SDK 支持的自定义压缩结果；手动/自动触发仍由宿主控制。辅助模型请求经过同一保存、敏感屏蔽与完整目标窗口门禁。无法证明来源、旧续接不可用、取消或窗口不足时显式返回 cancel；不能只抛异常，因为 SDK 会捕获扩展异常后继续默认压缩。

成功的原生 compaction entry 保存输入证明及输出来源标识；session_compact、capture/rescan 和 session_tree 按当前原生路径恢复该记录。task_id 取首条真实用户 entry 的稳定摘要，goal_role 为 initial_user_request；它是初始出处而非永远不变的当前意图。完整原生摘要作为低信任 progress 引用保留，默认完成状态 unknown，不能把助手的“已保存”当成实际文件写入或任务完成证明。

每个原生检查点拥有由来源会话和 compaction entry ID 派生的分支标识。选择依据当前原生路径上最后一个检查点，不按跨分支的“最新时间”选择；新的检查点通过 consumed_checkpoints 继承实际读取的旧记录的固定来源依赖。废弃、过期和来源失效记录只返回状态/定位，不回退旧正文。无摘要树导航切回旧路径仍读取它自己的头状态，不覆盖其他路径。原生 branchSummary 在普通上下文及两组压缩输入中均排除，不能借下一次压缩变成当前任务；UI原生日志仍按宿主保留。

installPiMemory(...).work(sessionId) 读取已捕获路径上的当前检查点，其他配对入口可通过 trusted-host continuation_current 读取同一 task_id/branch_id。新运行实例或扩展 reload 后先使用新生命周期上下文 capture/rescan；不得持有 SDK 已作废的 ExtensionContext。测试使用真实 SDK 的 reload、SessionManager.forkFrom、新 AgentSession 与无摘要树导航；不将其描述成桌面 UI 或全部 AgentSessionRuntime 交互验收。

## 原文、输出与来源控制

fork 首次启动时核对 parentSession 的真实继承 entry ID，复用原来的 session/parent/event 身份和序号；未知继承正文拒绝采集，不登记成新的子会话证据。验证通过后，普通账本中的不可变 fork-origins-v1 只保留来源标识、序号、parent ID 和正文哈希，最多1024条，不复制原文。重载使用这份证明核对继承内容；新增的子会话记录保持子身份。嵌套 fork 复用祖先已验证的证明，祖先原始文件后来损坏也不把 A 的来源迁移到中间 B。最多64层，单个首次核对文件最多64 MiB。

已有 fork 若缺乏可核对的首次继承证明，或首次核对前祖先已损坏/丢失/不匹配，保持 continuation_origin_unavailable 限制；不猜测时间戳或把全部未匹配消息当新输入。真实宿主应在 session_start 完成继承核对，再开始新输入。不能把这种安全限制描述成支持任意旧 profile 无损迁移。

message_end 绑定实际助手消息对象与已保存尝试，在该对象获得原生 entry ID 后写入稳定流链接；这早于工具采集历史的时刻，不按正文或时间猜配。依赖包括原生审计/文本记录、真实缓冲输出或可靠流最终提交消息，以及真正保存的工具结果。压缩的独立输出也作为依赖，不只依赖后来保存的摘要副本。

当前可见原生历史在采集时重新检查来源；失效内容进入既有安全裁剪，保留当前真实用户边界。辅助压缩请求包含不允许发送的原文时整个请求阻断。辅助调用绑定外层实际消费的续接快照，不能在内层换成已失效的新快照后声称旧正文仍有效；最终计数后还重新核对输入证明的原始来源版本及其不可变身份。普通请求在最终计数后重新核对可见历史和真实输入边界。来源恰好在计数期间变化时阻断本次请求，下次重新构造。不能撤回已经交付的内容，也不会因一条摘要来源被抑制而假称所有独立原文副本已物理删除。

## 预算与可查询状态

有续接时核心构造和续接选择分别使用至多一半 memoryTokens；组合整个 JSON payload 后再使用目标 tokenizer 计数。完整 provider context、宿主系统/工具声明、原生历史与输出预留使用 countRequest 及较小的配置/物理模型窗口验证。辅助压缩不额外注入核心画像/事实，而仅消费已验证的原生历史及旧任务数据。

共享 renderWork 先预算遗漏元数据，再选择完整正文；不足时保留紧凑检查点定位。普通 read 可按 message_id 回取原文，trusted-host continuation_current 可回取完整任务记录。核心小额度返回空 payload 时保留 context_envelope_over_budget，不伪装成证据完整。status.context.used_tokens 仍指核心，provider_payload_tokens 指整个合并后 payload；host settings 报告当前记忆、窗口、输出预留、7天TTL及当前原生可见投影256条记录的来源限额，核心合并依赖仍最多1024。无模型消息的会话元数据不计入该限额，context_edit 审计仍保留为依赖。

## 验证范围

公开核心和 stdio 语义由 #79 的回归覆盖，本票通过真实 pi SDK、合成 provider 和固定本地 tokenizer 验证原生接线。未测真实模型摘要/回答质量、桌面安装、多插件、规模、p95、系统断网/睡眠或物理掉电；不启用日常 profile，也不解除 #81/#82 的门禁。
