# T17 有界摘录导航与补跑

对应 [#18](https://github.com/destinyTaoTao/evernook/issues/18)。`evernook.maintenance.NavigationMaintenance` 在同一 SQLite 账本维护日、周与字面主题导航。处理器固定 `extractive-navigation-v1`，无模型/GPU调用，不接纳事实、不修改人设。摘录是导航入口，不能代替完整证据或充当执行指令。

## 明确配置和调用

可信本地操作端使用 `NavigationPolicy(offset_minutes=..., topics=(...), max_quotes=..., excerpt_chars=...)`。`offset_minutes` 必填，无默认时区；使用固定 UTC 偏移下的 **接收时间** 分桶，周一为周起点，不声称支持 IANA 时区或夏令时自动切换。每个摘录另保留来源 `occurred_at`，未知时保持未知，不能把接收日当作事件发生日。

每次 `tick(access, policy_id, budget=MaintenanceBudget(max_messages=..., max_jobs=..., timeout=...))` 显式给出预算。上限分别为 256 条消息及 256 条变更、16 个任务、5 秒协作期限；每个导航最多 64 条摘录、每条最多 512 code point。示例配置及 JSON 输入：

```json
{"offset_minutes":480,"topics":["合成主题"],"max_quotes":32,"excerpt_chars":512}
```

以上是合成示例，不代表已批准的产品默认值。CLI 命令均接受 `--config <外部运行配置> --access <外部本地访问文件>`，正文走 UTF-8 stdin：

| 命令 | JSON 内容 |
|---|---|
| `maintenance-configure` | 上述完整策略，返回不可变策略散列 ID |
| `maintenance-pause` | `{"policy_id":"...","paused":true}`；恢复为 false |
| `maintenance-tick` | `{"policy_id":"...","budget":{"max_messages":64,"max_jobs":4,"timeout":2}}` |
| `maintenance-status` | `{"policy_id":"..."}`；可选 after_bucket 对范围列表分页 |
| `navigation-read` | `{"policy_id":"...","bucket":"day:2026-10-30"}` |

相同策略重复配置幂等；调整参数得到独立的新策略，不混用旧空间。主题是最多 8 个字面字符串的不区分大小写匹配，非语义主题模型。时间、预算和主题均未在日常宿主中隐式启用。

## 补跑、并发和失效

- 发现阶段按账本 rowid 顺序有界扫描消息与显式变更，来源成员、范围队列和扫描游标同事务提交。休眠或关机后补扫描已有证据，同一天/周/主题合并为一个待更新范围，不为每个错过的定时点制造任务。游标只说明已扫描进度，不冒充摘要完成水位。
- 任务使用 30 秒租约；真实进程退出后租约可过期重领，重复执行替换同一范围的版本。派生处理使用 WAL 读快照；提交时检查范围修订、租约、暂停 epoch 与当前来源。并发新增或更正不能让旧任务覆盖新版本。
- 暂停只影响导航维护，原始采集、显式记住和现有证据读取仍可继续。暂停/恢复都会使旧 worker 失去发布资格。Python 调用可传 `threading.Event` 类型的 cancel / foreground，任务在扫描条目与处理阶段之间让步，发布前再次检查。
- 时间预算为协作期限，SQLite 锁等待、ACL、文件系统和汇总计数有各自成本，**没有严格 5 秒墙钟或前台时延保证**。本增量没有自动检测宿主空闲、操作系统计划任务或 CPU 占用率控制；需要由宿主在空闲时调度，不能用循环高频 tick 代替调度策略。
- 摘录只来自完整、原件已保存的用户/助手/工具正文，最多选择范围中较新的有限条目，显示 total_sources / omitted_sources。角色扮演、助手生成或恶意引用均保留为 `untrusted_source_quote`，不推断成现实事实。系统审计与受保护投影不进入摘录。
- 每条摘录附 message ID、状态/内容版本、表示、Unicode 坐标和原话。导航含扫描水位、生成时间与处理器版本。每次读取及发布都重验来源；更正、撤销或抑制使现有导航立即 stale，包括变更尚未被后台扫描、以及变更目标原先因预算未被摘录的情况。旧文本不再返回，后续 tick 重建范围。
- 协作式让位（canceled/foreground/time_budget）把任务健康地放回 queued 且 `error_code` 为空，只有真实失败类别（`processor_failed`/`stale_source`）写入 `error_code`。处理失败仅持久化固定 `processor_failed` 类别，下次有界调用可重试；原始异常不进入导航。真正的存储故障可能无法写失败标记，此时保留 running/租约并由过期重领恢复。`read` 可区分 queued/running/expired/succeeded/stale/failed。

本增量没有生成式情景摘要、语义主题聚类、自动事实接纳或 T18 经验规则启用，也未接入原生 harness/context-builder。新收到但尚未扫描的记录会通过 status 的 observed head 和 cursor 差异显示积压；旧范围摘要仅代表其已标明的扫描进度。永久删除、控制记录恢复重放及全量派生迁移仍由后续票据完成，不能依靠导航替代这些入口。
