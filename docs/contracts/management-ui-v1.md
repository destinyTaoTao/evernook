# 可信记忆管理页面 v1

对应 [#89](https://github.com/destinyTaoTao/evernook/issues/89)，属于 [Spec #88](https://github.com/destinyTaoTao/evernook/issues/88)。`MemoryManagement` 与可信本地 CLI `manage` 复用同一 SQLite 权威账本，不建立 HTTP 服务，不注册 MCP 或模型维护工具。

调用身份须同时持有 recall/maintain/accept。宿主必须使用与普通 capture/recall 身份不同的维护身份，并在已鉴权、受跨站保护的页面通道中建立真实用户意图；持有维护凭证的本地代码属于可信边界，不防御同账户恶意进程。不能把来自模型的 approved 字段直接转成确认调用。

## 查询

`{"action":"read","view":...}` 提供 profiles、drafts、suppressed、history、timeline、source、status。列表最多50项，默认25，按 next_offset 继续；由于来源过滤允许空页带下一游标。source 每页2048 Unicode code points，用 next_cursor 读取余下内容。画像列表不复制证据正文，按来源ID另行分页。timeline 支持 session_id 与最长256字符的字面子串 query，不冒充语义检索；发生时间未知保持 null，记录时间单列。普通管理读取不绕过来源抑制或提供保险库明文。

## 预览与确认

`{"action":"preview","operation":...}` 支持 source_suppress、source_correct、profile_confirm、profile_reject、profile_suppress、profile_revise。源操作带 message_id，更正另带 text；画像操作带 tag_id，修订另带 label。未知字段/操作、敏感新正文、受保护来源修改、争议澄清画像均拒绝。

预览绑定调用端、操作HMAC、目标和证据版本，来源操作还绑定已知画像/事实/经验依赖ID与修订；超过依赖上限明确失败。预览有效期5分钟，每端最多64项待确认。持久表只保留HMAC、ID、版本和精简回执，不另存证据正文。详情摘要明确 total，完整来源另行分页。

`{"action":"confirm","proposal_id":...,"operation":...}` 必须由可信宿主在用户确认后调用。完全相同的操作才匹配；提交前同事务重新检查预览有效期、来源和目标状态及版本。确认记录、原话替代/画像决定、来源控制与实际回执同事务提交；失败不留下半次确认。相同调用端和操作重放返回原精简回执，结果不确定时可用此路径回查；已经完成的回执不保证目标此刻仍处于同一状态，当前状态须重新查询。

画像修订使用用户在页面输入的新标签作为新的用户证据，并在本次真实确认的同事务内完成修订与确认；不改写旧原文。来源更正将新用户原话追加为替代来源并保留旧档。移出画像不禁止原话检索；来源抑制不承诺屏蔽其他独立来源或全库同义表达。已经交给宿主/provider的内容不会被撤回。

`rejected` 视图列出原始用户拒绝，`profile_reopen` 将其经真实用户确认重开为草案；继承拒绝的新tag不能代替原始决定重开。跨处理器防重复用真实拒绝事件，按类别、规范化标签和来源版本/片段确定候选身份；不承诺开放域同义屏蔽。超过每类1000条拒绝检查上限明确失败，旧并行草案不能通过确认绕过拒绝。详见 [#90记录](../reports/dsh-ui-02-rejection.md)。

物理删除、恢复、独立事实决定、争议裁决、人设编辑和保险库明文不在本页面入口范围内。本契约不把接口存在当成安装、浏览器或正式日用验收。
