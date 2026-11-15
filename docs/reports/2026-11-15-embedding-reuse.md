# #84 / #124：有界本地嵌入复用

实际固定 MiniLM 的 31 次调用中，一次性路径启动 31 个 Node，复用路径启动 1 个；两路径的全部向量（含超出 tokenizer 限制的 null）与此前保存的基线逐值相同。去掉首次调用后的 30 个样本，一次性路径 p50/p95 为 1023.18/1077.23 ms，复用为 218.79/246.83 ms。这里仅测公开 embedding 调用，不代表完整记忆或规模查询达标。

`LocalMiniLM.embed` 保留原一次性行为；显式 `session` 和服务持有的 `ServiceEmbedding` 可复用进程。会话最多 120 秒，逐请求仍检查全部固定模型摘要、敏感输入、时间预算、关联 ID、输出大小、向量维度与归一化。并发请求串行执行；排队者取消不终止他人的在途请求，在途失败关闭当前 worker。服务关闭回收会话，轮换后重新载入。Windows wrapper 使用现有真实父进程句柄及 KILL_ON_JOB_CLOSE 作业对象，覆盖宿主被突然结束的情况。

没有缓存原文/查询结果、修改嵌入空间、模型、池化、阈值或默认模型准入。反复使用 120 秒请求预算可能每次轮换；复用收益适用于能在剩余会话期限内完成的请求。逐次模型完整性读取仍有成本，未改成仅按 mtime 信任。

验证：session/provider/semantic/window 联合 44 项通过；其中 7 项 session 回归覆盖资产改写、受保护输入、超时、在途/排队取消、进程崩溃、错误 ID、超大回复、轮换、显式关闭与宿主突然退出后的真实后代句柄。早先 service 配置/生命周期定向检查 11 项通过，与上述有交叠。相关 strict mypy 通过。另有非 editable wheel 安装验证，结果在本轮最终规模报告汇总。

外部证据在本轮 campaign 的 `minilm-baseline`、`minilm-session-final` 和 `minilm-session-final-monitor`。最终监控窗口 40.069 秒、160 次采样、观察到的进程树 Private Bytes 峰值 689328128，1 次查询失败；这是采样结果，不能保证硬峰值或作为完整资源准入。原始向量、逐次时延、worker 启动轨迹均保留在 checkout 外。#84/#124 与日用门禁继续开放。

独立收尾复核另复现了非法 worker 向量含 `10**400` 时浮点检查抛出原生 OverflowError、会话未关闭。共享 `unit_vector` 现将越界规约为 `invalid_embedding`，一次性及复用路径语义一致，复用路径按既有失败分支关闭。新增真实合成 worker 回归先失败后通过；session/provider/semantic/window 最终联合 45 项通过，相关 strict mypy 通过。正常模型数值、协议和嵌入空间未改。
