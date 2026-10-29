# T08 工具事件、文本附件与采集缺口

对应 [#9](https://github.com/destinyTaoTao/evernook/issues/9)。统一本地 Python 边界新增 `submit_capture/read_capture/read_attachment`，所有操作沿用身份/版本检查；握手 `captures=true,storage_version=4`，事务补表兼容 v1–v3，不重写既有记录。没有安装真实 dsh 插件或提供网络上传端点。

## 采集信封与可靠保存

```python
from evernook import AttachmentInput, Capture, Event, Source

# memory/access/input_receipt 来自已保存的合成输入。
capture = Capture(
    Event(Source("synthetic-dsh", "s1", "main", "tool-result-1"), "tool", 2,
          None, "complete", "合成工具结果：37.50元。"),
    "tool_result", input_receipt.message_id, "synthetic-tool",
    (AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic", b" attachment")),))
receipt = memory.submit_capture(access, capture)
assert receipt.complete and not receipt.gaps
assert memory.read_attachment(access, receipt.message.message_id, "a1") == b"synthetic attachment"
```

`kind` 为 `message/tool_call/tool_result`。工具事件必须指定已存在的关联输入 `input_id` 和稳定 `tool_name`，保留实际角色、来源四元组、分支、顺序、源时间与完成状态；关联可以跨分支，但分支仍是各事件实际来源，不因此变为当前有效事实。这里只存证据，不执行工具、推断调用成功或把分支输出当事实。

同一来源核对完整事件、种类、工具/关联输入、附件元数据和内容的受密钥保护指纹；同请求并发/重启重试保持消息及任务唯一，同键异内容报冲突。附件不同块切分但相同 bytes 视为相同内容。切换普通 `submit` 与采集信封不视为同一请求。

可接受附件 BLOB、清单、消息与一个待处理任务在同一 SQLite FULL 事务提交；数据库位于 checkout 外且继承已有 ACL。不引入第二对象目录或文件暂存区，因此该路径没有事务外孤立文件：关联/附件插入失败或提交前进程退出整体回滚，重试即可；不会返回半关联成功。提交后的每次清单/附件读取重新核对清单尺寸与 SHA-256，损坏/缺失显示 `attachment_corrupt/attachment_missing`，不再报告完整；不自动覆盖修复旧证据。

## 支持范围与缺口

每信封最多 16 个附件。首个实现仅接受单个不超过 1 MiB、可严格 UTF-8 检查的 bytes，支持 MIME `text/plain`、`text/markdown`、`application/json`；按文本检查，不承诺 JSON 语义校验。附件 ID/标签/MIME 为非敏感稳定元数据，不作为文件路径使用。`expected_size/expected_sha256` 可选，用于核对入口声明；`complete` 表示附件传输是否完整，`chunks=None` 明确入口未提供。

`CaptureReceipt.message` 只描述**消息正文**保存；必须同时检查外层 `complete/gaps` 和各 `AttachmentInfo.saved/gap`，不能把正文 `committed` 当成全部附件完整。已提交缺口可按消息 ID 经 `read_capture` 查询，包含原始文件尺寸（已收到时）、来源和完成状态，不保存不可检查原件或其公开低熵散列。

- `entry_not_provided`：正文/附件没有取得；正文 `provided=False` 时须传空文本。
- `incomplete_output/incomplete_attachment`：输出未正常完成或附件未传完；不推断成功。
- `size_limit`：正文或附件超过支持预算；不静默截取为完整原文。
- `unsupported_format/invalid_encoding/integrity_mismatch`：不可检查格式、非法 UTF-8 或入口声明与实际内容不符。
- `protected_original_not_saved`：声明/检测为敏感，保险库关闭，原件未保存。

未知/非法信封、重复附件 ID、错误字段类型、敏感元数据、关联输入不存在会明确拒绝而非创建半条消息。PDF、图片、音频、压缩包等当前不具备安全检查器，只记录不支持缺口；不落普通附件原件/索引，不假称多模态留存已完成。

## 分块敏感与读取边界

正文可传 `text_chunks: tuple[str,...]`（最多 4096 块，此时 `event.text` 必须为空），在内存合并后统一检查，分块不会先进入普通账本、日志、模型或显示。完整流可保留检查后的原文；未完成/失败/取消的分块无法判断后续是否拼成秘密，因此保守只保存占位投影并明确缺口。此接口接收已由调用端收集的有界块快照，不实现持续上传会话或逐 token 显示；调用中断且尚未确认时用稳定来源重试。实际全部展示通道及恢复回放仍由 T10 验收。

附件完整 bytes 合并后检查跨块凭证信号；正文受保护来源/标记或检测为敏感时，附件也不保留原件。独立附件声明敏感或检测到秘密，同样只登记未保存。密码前缀跨块、损坏和超限材料不会落普通对象；指纹使用已有独立 ACL 保护 HMAC 密钥。凭证检测仍有有限覆盖，URL 协议名前缀限制为 32 字符以避免超长负例反复回扫；不能以此声称任意秘密零漏检。

`read_capture/read_attachment` 检查目标消息的 T07 状态，被更正/撤销/抑制的目标不可从这些新入口绕读。原始工具事件仍是不可信证据，不能作为更正的用户指令来源；多来源派生事实及关联事件级联规则仍须未来流水线明确，当前不自动推断。模型、保险库、备份和实际宿主接入未启用；备份目的地/启用/恢复验收继续待办。
