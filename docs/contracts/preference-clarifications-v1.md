# M58-18 偏好争议与一次澄清

可信本地接口 `PreferenceClarifications.open(access, OpenClarification(...))` / `resolve(access, ResolveClarification(...))`，位于 evernook.preference_clarifications；均要求 accept 权限。CLI 为 preference-clarify / preference-resolve，沿用 --config / --access 与 UTF-8 JSON stdin。普通 MCP 没有写入口。

这一步由可信调用端明确指定同一主体、同一偏好槽和关联画像，建立实际用户意图；不从两个不同兴趣或工具名推断它们必然互斥，不自动判断同名主体。自动检测、宿主展示与交互由后续闭环复用，不据此宣称模型已能识别所有语义冲突。

## 发起

OpenClarification 字段：subject_id、predicate、profile_tag_id、candidate_id、confirmation_id、expected_fact_revision、expected_profile_revision。confirmation_id 是可信调用端已建立意图的用户证据引用，表示确认需要保留争议/澄清，不是接纳新值。

要求已有当前可用的事实及已确认画像，二者来源相同；新候选须为三类有来源偏好之一，属于对应 preference / interest 画像类别。一次 SQLite 写事务追加 disputed 事实并将同一画像修订为待确认草案。旧事实及画像历史保留原来源，新争议/草案保留新来源，双方不被覆盖。任一来源或画像 CAS 失败整体回滚。

clarification_id 由本地密钥、主体/槽/画像/新候选身份派生；相同输入重启或并发重试返回同一未决回执，不再增长修订。同槽已有另一未决争议时明确返回 preference_clarification_pending；新增候选原文继续保留，不合并为已处理。已解决争议不能用旧 open 请求重新开启。

## 暂停与确认

未决画像为 draft，summary 不返回它。上下文中的争议保留 state / clarification_required 与可读取的争议来源，fact 字段置空；原先陈述仍可通过明确的事实/知识历史查询查看，不能作为确定偏好注入。构建上下文期间事实水位或澄清状态变化，最终快照会丢弃旧条目。整个 payload 仍为 untrusted_memory_context，不改变宿主本轮原生消息或授予原文指令优先级。

ResolveClarification 必须明确提供 subject_id、predicate、profile_tag_id、clarification_id、confirmation_id、expected_fact_revision、expected_profile_revision、kind 和 choice。choice 为 incoming / existing；kind 为 correction / change。

- incoming + correction：采用新说法，更正旧陈述，继承原有效时间。
- incoming + change：明确现实发生变化，须给 valid_from，晚于原起点且不能晚于当前时间。未来变化需要计划路径，本入口不提前激活画像。
- existing + correction：用户明确保留旧说法，以新决定解决争议，保留旧来源/时段；不是因新来源失效自动回退。

确认消息必须在待澄清草案之后保存。一次事务追加带 resolves 的事实并通过受控 clarify 动作确认选中的画像；clarify 核对同一事实解决记录、同一确认消息、相同文本和来源。普通 confirm / auto_confirm / revise / reject 不能绕过待澄清画像；尤其原 interest 自动确认路径不能跳过争议。保留旧值也通过同一用户确认流程。

重试复用同一解决与画像回执；改变选择、kind、时间或确认会冲突，不默默覆盖。旧画像后来被改动或抑制、来源/确认失效时不能用旧解决请求恢复它。事实仍区分有效时间与 recorded_at 知识时间，画像历史保留各版草案/确认。

## 删除与边界

未决争议的来源被抑制或删除，正文不再返回，但无正文争议控制仍使 clarification_required=true；这不构成对竞争旧说法的确认。来源失效会阻断旧解决；删除后的事实墓碑保留该控制，不会复活旧偏好。重新启用需要新的有效来源及可信审查，不能靠重试猜回已删片段。

没有新增影子冲突表：沿用事实/画像事件与现有控制重放。未验证宿主自动展示、真实模型理解澄清或桌面日用行为。本轮验收为公开 API / 真实 CLI、合成证据、确定性并发及事务故障边界。
