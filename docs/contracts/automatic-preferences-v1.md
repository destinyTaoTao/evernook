# 三类偏好自动更新 v1

## Core0.1.17：完整段落及范围

#113沿用三类有限规则、默认未准入及可信配置边界。当前规则从完整原文提取第一人称子句，保存精确非零跨度；不是任意自然语言理解模型。一般/平时等长期陈述与这次/本轮等临时例外分开处理，临时、过去与计划范围回执为ignored，不进入稳定画像。逗号前仍有效的时间限定不能丢弃。

整段引用、转述、条件、不确定及高风险语境保守阻断；无法解析的第一人称否定也阻断该段，避免只截取肯定子串。同段同槽冲突不发布任一肯定，有旧值时暂停旧值并保留争议。明确否定纠错仅在否定当前值时返回withdrawn，复用dispute/profile draft；不抑制、修改或删除原始消息。否定其他兼容值不撤当前偏好。历史长段落草案同样参与冲突检查。

处理器身份同时绑定规则、完整上下文解析器和既有抽取实现字节。升级不继承旧准入；可信维护者需重新选择当前身份，不能由模型自行批准。自动提取、候选数、合并/撤下标签数、澄清与忽略分别在process.processing报告；source_contexts包含来源元组、角色、发生/获知时间、控制/内容版本及范围。observations_stage=not_run明确尚未执行#118。

独立服务支持上述preference-update-*及preference-clarify/resolve可信CLI，普通capture/recall身份无法配置或发布；profile-summary可通过服务读取。dsh下一次构建背景时读取当前版本，不撤回宿主先前已持有的历史消息。实测见[本票记录](../reports/automatic-profile-2026-11-13.md)。

对应 [#78](https://github.com/destinyTaoTao/evernook/issues/78)。公开本地入口 `evernook.automatic_preferences.PreferenceUpdates`；普通 MCP 没有发布、配置或准入工具。自动处理必须由持有 recall/maintain/accept 的独立可信工作身份执行，普通 dsh/pi 采集与模型身份仍只有 capture/recall。

## 准入与配置

复用全局 consolidation tier：只有 auto 档才允许自动发布，manual/review 均停止发布。另须显式准入当前有限规则处理身份，并选择三类中的子集：response_style、technical_tool、general_interest。默认未准入。`PreferenceUpdates.configure(access, enabled=..., categories=(...), admitted_processor=...)` 是完整的可信配置操作；enabled=true 设置全局 auto，false 将 auto 切回 review，已有 manual 保持 manual。categories 不可包含身份、关系、健康、财务、规则、经验或核心人设。

既有 consolidation-configure.profile_categories 与此配置共同收窄有效白名单，设为空立即停止对应发布。旧 interest 配置只映射到 general_interest，不扩大为三个类别；新默认枚举为三个具体类别。通用 consolidation profile_tag 提案始终保留草案，不能借旧 interest 自动确认路径绕过新检查。

CLI 均使用 --config / --access 与严格 UTF-8 JSON stdin：

| 命令 | 输入 | 结果 |
|---|---|---|
| preference-update-status | {} | 实际生效设置、当前规则身份、处理回执计数、待复查草案数 |
| preference-update-configure | enabled、可选 categories、admitted_processor | 保存后的实际设置 |
| preference-update-process | source_ids、可选 timeout | 逐来源独立 runs 及各项处理回执 |

仅能准入当前内置有限规则的完整身份；未知身份拒绝。身份前缀为 automatic-preference-rules-v1，绑定规则/发布实现字节及共享偏好分类策略，不把任意模型合法 JSON 或置信度当准入。代码/策略变化后旧准入不能自动沿用。准入前应核对固定对抗回归与本票验证记录；仓库实现不替可信维护者伪造审查或模型质量成绩。固定 Qwen 仍是未准入的候选后端，不因有限规则通过而转为自动接纳。

## 输入与决定

沿用[候选契约](candidates-v1.md)和[完整陈述识别](preferences-v1.md)：真实 user 角色、完整且可核对的原文、当前有效 state/content version、单个完整来源、无引文/角色扮演/助手猜测/假设/混合高风险内容。只认识有限工具、兴趣和回答形式，未知表达继续保留原文或草案。源时间晚于接收时间的记录保持 future_source 草案。裸“我希望…”可能只是当前轮要求，保持 preference_scope_unclear 草案，不改写或争议化长期偏好。

回答详略和排版分别使用槽；不同工具/兴趣可共存。同一工具的版本变化保守进入澄清，完整原话和版本仍保留。字面否定只参与冲突控制或审查，**从不自动接纳为新偏好**；例如“我不喜欢观鸟”可暂停已接纳的观鸟兴趣，不能丢掉“不”后当成正向兴趣。相容的否定不会被强行判为冲突。这些有限规则不是开放域语义矛盾检测。

无冲突的合格肯定陈述在同一 SQLite 事务中追加事实、画像及处理回执；事实使用 user 主体下 preference:* 的保留槽，画像有稳定 HMAC 标识。直接陈述是自动政策的证据，不能描述为额外的人工确认。重复来源/同义有限形式不增长事实和画像版本。发布失败整体回滚，可按原候选重试。

一次 process 最多16个来源、合计16384 code points，并共享总 timeout；每个来源创建独立候选运行，runs 可逐项核对。无关来源的抑制/删除不会使其他来源的有效否定控制一起失效。发布事务开始和提交前复核截止时间与取消信号，超时不发布；已成功的提取仍可留下可核对的候选运行。部分失败通过各 run/error_code 披露，不把批量请求描述成全库事务。预算在协作边界检查，不承诺中断正在执行的 SQLite 调用。

已有事实与画像时复用[#76联合澄清](preference-clarifications-v1.md)：保留双方来源、一次未决争议、暂停确定偏好，实际用户确认后才更正或变化。两个尚未接纳的相反草案，以及只有既有用户确认画像的情况，使用一个稳定画像审查草案，保留双方来源/历史；通过可信 profile-decide 修订选择并在真实确认后生效，不虚构一个先前已接纳的事实。重复陈述复用待办标识。已有抑制、拒绝、来源失效或不明旧画像时不擅自恢复；无法分类或超过检查上限时保持审查。

保留旧值或采用新值后，已裁决来源重放返回已生效/已审查回执，不重新开启争议；真正晚于确认的新陈述仍可触发新澄清。v1 的槽含义保持稳定，代码身份变化不把旧有效规则草案当作没有冲突；旧规则控制须按当前完整来源与语法复核。更改槽含义需要新的契约版本及迁移审查，不能只替换代码后沿用旧准入。

当前轮原生指令保留在宿主消息中。长期记忆始终是 untrusted_memory_context 数据；画像抑制同时阻断关联自动偏好事实的确定性注入，并在最终可见性快照中复核画像状态/修订、事实水位和来源。明确的历史事实查询仍遵守独立读取契约，不把上下文排除误称为撤回宿主已持有的历史。

## 后台与可观察性

复用[宿主调度器](host-consolidation-v1.md)及其全库租约、前台优先、暂停和预算。处理选中原始来源时先完成相应规则草案/发布，再确认原始沉淀进度。准入后已有合格草案可在后续触发晋升，不必等待新增消息；与当次原始窗口合计不超过来源额度。数量门槛控制新原始任务，已有草案晋升仍遵守暂停、前台、空闲等待和时间限制。旧版已完成主题沉淀的来源不静默批量重新解释；需要重新提取时使用显式 process 入口。

处理回执与 source/candidate ID 可核对，state 表达 draft、merged、pending_clarification 等；回执描述处理结果，不能代替当前 profile-read 的可见性检查。status.historical_receipts 统计最新保存的处理回执状态，pending_drafts 是其草案数量，queued_for_recheck_up_to_256 是受当前策略允许进入复查队列的有界预览，不表示这些记录已经通过来源/冲突检查。scheduler-status 同时报告原始积压、索引覆盖与本处理状态。词法索引未建或滞后不会因自动发布而冒充追平。

删除候选运行前同事务清理关联处理回执；未决争议与来源控制继续按原删除契约执行。运行数据、令牌、模型和准入配置均留在仓库外。不启用安装 profile、云端降级、系统任务或模型自主维护权限。

验证覆盖公开核心/真实 CLI、SQLite 回滚、处理中抑制以及锁定 dsh/pi 实际 provider。dsh 采用 durable_text；pi 原生维护确认采用 buffered 模式，因为当前 text-only durable 模式拒绝注册维护工具。单独的可靠文本流回归继续保留；本票不宣称这两个 Pi 模式已完成组合日常验收。
