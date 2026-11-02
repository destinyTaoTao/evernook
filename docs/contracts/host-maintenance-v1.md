# 原生宿主确认式维护 v1

对应 [#69](https://github.com/destinyTaoTao/evernook/issues/69)、[#70](https://github.com/destinyTaoTao/evernook/issues/70)。dsh `0.2.1-rc.3` 与 pi `1.4.0` 显式 SDK 接线共用 `maintenance.ts`，不自动修改或启用日常 profile。普通采集继续使用独立 `capture`/`recall` 身份；模型不能取得维护凭证。

## 可信接线

先用同一 `MemoryConfig` 安装 `installMemory(ctx, config)`，再从 `@evernook/harnesses/dsh-maintenance` 调用 `installDshMaintenance(ctx, config, memory, options)`。`options.access` 必须是仓库外、ACL 保护、预先配对的独立维护身份文件，包含 `recall`、`maintain`、`accept`、`admin`；它不能与普通连接共用 client ID。安装只检查身份，不配对或提升权限。路径、Python、工作目录和回调在安装时固定。

必填的 `onReview(review, signal)` 由可信宿主展示完整操作、来源摘要、版本、旧值/候选、删除范围和残留，以及 `confirm_command`。来源摘要最多 2048 个 Unicode code point，标明总长与截断；宿主用 `readSource(message_id, cursor, limit)` 分页查看可见原文，不能把截断摘要称为完整内容。回调成功只表示预览已展示，不表示授权。回调错误仅向模型返回固定错误码。

用户随后在同一原生会话输入精确命令 `/memory confirm <proposal_id>`。适配器先保存真实 `source.kind=user` 输入，核验角色、完成状态、接收时间和会话，再经固定本地 CLI 执行。助手复述、工具结果、聊天中的管理员声明和通用自动批准均不构成确认。`onResult(outcome)` 必须展示实际结果或错误及残留；展示回执失败不会撤销已经提交的操作，可信宿主可用 `status(proposal_id)` 读取缓存结果。

模型只有 `evernook_memory_propose` 工具，接收 JSON 字符串 `proposal`，返回待确认 ID 和操作类型。没有确认或执行工具。宿主也可直接调用 `propose(agent, proposal, signal)`；仍执行相同预览和后续真实用户确认。

pi 在 `installPiMemory` 的配置中显式提供 `maintenance: { access, onReview, onResult }`，并用 `noTools: 'builtin'` 保留经过保存屏障包装的提议工具。返回对象的 `maintenance.propose(sessionId, proposal, signal)` 用于可信 UI；确认仍需当前分支真正落盘的原生用户消息，在最终可取消 provider 边界执行。`readSource`、`readImportant`、`status` 和回执结构与 dsh 相同。`createMaintenance` 的 `confirm` 是可信宿主内部接口，不得注册为模型工具；普通调用端不能用传入的消息 ID 代替实际原生输入选择。

两端可信 UI 可 `await reject(proposal_id)` 拒绝尚未执行的预览，释放待确认项；之后原命令无效，回执状态为 `rejected`。它不接受执行中或失败后的不确定决定，也不撤销已经提交的操作。回调和权限检查不因为非交互模式而跳过。

## 操作与不变量

| proposal | 行为 |
|---|---|
| `pin` + `message_id` | 将完整原件立即标为 shared 重要交代，不依赖后台沉淀；通过 `readImportant(important_id, cursor, limit)` 逐字分页 |
| `suppress` + `message_id` | 抑制该来源及依赖它的可召回资产，保留原档；不是单独抑制某一事实而保留其同源资产 |
| `correct_source` + `message_id`/`replacement_id` | 使用已保存、完整的真实用户替代来源更正；确认命令不会成为替代正文 |
| `profile` + `tag_id`/`action` | 沿用核心 `revise`、`confirm`、`suppress`；修订带 `draft`，先成为草案，之后单独预览并用新的用户输入确认才能生效 |
| `fact_correction` + `subject_id`/`predicate`/`run_id`/`candidate_id` | 从既有完成候选中更正当前事实，保留被更正事实的有效时间、发生时间及类别；争议要求澄清，不自动解决 |
| `purge` + `source_ids` | 最多 16 个精确来源，先持久化核心预览，确认时复查范围、版本与依赖；受保护来源不走普通删除 |

版本变化使旧预览失败，不自动扩大范围或覆盖新版本。更正/抑制影响后续核心读取和下一份上下文，不能撤回已经交付给 provider 的内容。彻底删除回执披露既有归档/备份保留及宿主副本不受控制；宿主历史和其他插件副本不会被自动擦除。事实候选生成、首次接纳、人设修改和保险库明文授权不在本入口范围内。

## 取消、重试与生命周期

提议最多 64 个未完成项，另缓存最近 64 个成功结果。预览和待确认状态只在内存中，重启后必须重新预览；核心已提交结果仍持久存在。重复组装不会因同一条确认输入重复尝试；失败重试需要新的实际用户确认，核心保持同一请求/确认标识以处理不确定提交。不能将超时或取消当成数据库回滚证明。

`close()` 撤销工具及钩子，取消并等待运行中的维护子进程退出，再关闭普通连接。预览期间取消后不建立待确认项。操作通过无 shell 的固定命令执行，正文仅走 UTF-8 stdin；输出有界，超时或取消会终止子进程。进程中断后若结果不确定，先读取实际核心状态，再重新审查。

真实 Session/AgentLoop/Tools → stdio MCP/可信 CLI → SQLite 已用合成数据验证；这不代表实际桌面 UI 的预览呈现、多插件组合、系统断电或真实模型质量已验收。
