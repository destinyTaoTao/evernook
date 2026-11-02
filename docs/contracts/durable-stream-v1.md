# 可靠文本分块 v1

对应 [#71](https://github.com/destinyTaoTao/evernook/issues/71)。这是可信本地 Python/CLI/MCP 的文本流持久化入口，尚未替代 dsh/pi 的整段缓冲模式；两端逐块发布由后续票据接线。核心不显示文字、不执行工具，也不能撤回宿主已经发布的副本。

## 调用和回执

`MemoryRuntime.append_stream(access, event, index=0, carry='', input_id=...)` 接受真实助手 `Event`。来源四元组、原始 sequence/occurred_at 和关联输入在整条流中固定；`input_id` 必须是已保存完整原件的真实用户消息。入口身份必须匹配 source.entry，普通 capture 身份即可写，不需要 accept/admin。index 从 0 连续递增；每块最多 64 KiB UTF-8，整条流最多 1 MiB、4096 个数据块，另允许一个不带新文本的终态控制块，以确保到达上限后仍可停止或正常结束。

Event.text 是新到达文本；completion=partial 表示继续，complete 表示正常结束，canceled/failed 为明确未完成终态。每次调用在同一 SQLite FULL 事务里提交安全片段、序号、受密钥保护的请求指纹、尾部校验和回执。`StreamReceipt` 包含：

| 字段 | 含义 |
|---|---|
| stream_id / index | 固定持久身份与本次块号 |
| state | open、complete、canceled、failed 或 protected |
| released_chars | 本次 `carry + event.text` 已提交、允许发布的 Unicode code point 数 |
| committed_chars | 整条流已提交的累计字符数 |
| pending_chars / gaps | 未发布尾部长度及缺口；不是已保存原件 |
| final_message_id | 仅正常结束时生成的完整普通助手原文 ID |

宿主只可在成功回执之后发布 `Array.from(carry + text).slice(0, released_chars)`（Python 用 code point 切片），并保留其余尾部用于下一次调用。不得直接发布原始 delta，或将 pending_chars 报告为已保存。宿主自行管理显示偏移；提交后、显示前崩溃可能留下已存但未显示片段，不能声称恰好显示一次。

同一来源/块号/完整请求重试返回原回执；异内容报 stream_chunk_conflict。跳号、旧来源身份变化和终态后新增块拒绝。正常完成时，完整正文与最后一块同事务建立，可经既有 read 回取。未完成片段以独立流状态保存，系统标记消息不是完整助手回答，不进入普通证据注入。

开始前拒绝已被普通消息占用或已有删除墓碑的原始来源；流开始后占用该来源，普通 submit/capture 不能另写一份答案。存储升级为 v5，使旧实现拒绝打开，避免绕过新增来源占用规则；v0–v4 由当前实现加表升级，既有原档不重写。归档读取兼容 v4/v5，运行库发布仍核对当前持久状态。不要以旧二进制打开已升级的运行库；控制检查点按当前代码和实际库状态重新取得。

升级前必须停止所有访问该库的旧宿主/MCP/维护进程，再用当前版本打开；不支持新旧版本同时运行或滚动升级。版本门禁阻止旧程序新开连接，不能让已经运行的旧代码自动失效。升级前备份保持原有审查/残留边界。

## 敏感尾部与停止

沿用现有有限凭证检测器，在块边界保留可能形成关键词、token、Bearer、私钥头或带凭证 URL 的尾部。普通中文/已消歧文本可以逐块提交，尾随英文单词等可能延迟至分隔符或正常结束。未消歧尾部原文只由宿主持有，SQLite 保存 HMAC 与长度；下一块必须提供匹配 carry，不能通过重启丢掉上一块后绕过组合检查。敏感信号出现后 state=protected，当前敏感尾部不保存原件、不允许发布，也不生成完整助手回答。

宿主丢失 carry 时，不能继续或伪造正常完成；可用相同固定 Event 元数据、空 text/carry 和 canceled/failed 提交停止，保留累计已提交部分并披露丢失尾部长度。取消/失败不会自动发布待消歧尾部。数量/尺寸超限或写入失败不前移序号，宿主必须阻断对应发布，可用同一块安全重试。不是任意秘密零漏检保证，也没有跨独立流或跨任意用户消息的全局语义检测。

## 回放、控制与恢复

`read_stream(access, source, cursor=0, limit=2048)` 返回来源、终态、完整性、已提交正文范围、next_cursor、next_index 和未发布尾部长度；limit 最大 4096 code point。open 在进程退出后仍明确是未完成，不推断正常结束；回放不会恢复未持久化尾部。来源输入、流标记或最终回答被抑制/更正后拒绝流回放，旧回执也不能绕过控制继续写入。

流标记是普通 capture，关联真实输入；正常完整助手记录依赖流标记。删除仍需完整精确依赖范围及后续用户确认，不自动扩大范围。删除流标记/完整回答时同事务清理分块数据，原始来源墓碑阻止重建；备份和宿主残留沿用既有契约。普通归档包含流表，可信控制检查点覆盖流状态；运行库发布还逐表比较持久流内容，旧快照缺少新块时拒绝发布。

## 传输

- CLI `capture-chunk --config ... --access ...` 的 UTF-8 stdin 为 `{event,index,carry,input_id}`；`read-stream` 为 `{source,cursor?,limit?}`。
- MCP `evernook_v1_capture_chunk` 仅 trusted-host profile；`evernook_v1_read_stream` 是普通 recall 查询。原 `capture_stream` 仍表示整段缓冲，语义不变。
- TypeScript `MemoryClient.appendChunk(event,index,carry,input_id,signal)` 与 `readStream(source,cursor,limit,signal)` 使用同一核心；握手 durable_text_streams=1。凭证由可信配置加载，不进入模型参数。

真实进程退出、SQLite 故障、重投、跨块敏感信号、CLI/stdio 和恢复库测试使用合成数据；尚未证明物理断电、日常 UI、多插件或实际 provider 的发布钩子，不能将本票当作两端可靠实时展示验收。
