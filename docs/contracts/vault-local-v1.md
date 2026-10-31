# T23 单次本地查看增量

对应 [#24](https://github.com/destinyTaoTao/evernook/issues/24)，在 [受保护收件箱](protected-inbox-v1.md) 之上提供显式启动的本地 Tk 窗口。它不是模型工具、服务端解锁会话或 harness 明文交付协议。

```text
<python> -m evernook.cli vault-show --config <runtime.json> --access <access.json> --inbox-dir <inbox> --key-dir <keys> --object-id <UUID> --purpose <本次非敏感用途>
```

主口令没有参数/环境变量/stdin路径，只能输入该窗口的隐藏 Entry 控件。窗口固定显示调用端、对象、版本、操作、用途和本地目的地，用户点击“确认本次用途并仅在本地展示”后，才进行本次验证和解密；不接受 `approved=true` 或模型声称用户同意。调用端使用已配对访问文件，沿用收件来源入口限制。

每次启动创建一个绑定对象、密钥/来源版本和精确签名信封的请求，有效期120秒。错误口令、撤销、取消、过期、解密或交付失败均消费本次请求；线程内并发重试最多一次交付。审计为尽力而为：审计存储不可用不掩盖真实拒绝码（如 `vault_unlock_refused`/`vault_source_unavailable`）；Python 构建缺少 tkinter 时 CLI 报 `vault_display_unavailable`。解锁不授予全库访问，也不留下通用明文读取令牌。明文只进入本地 Label，不写 stdout、剪贴板、账本、FTS或审计；CLI结束只返回请求ID和窗口关闭状态。

ACL检查、主口令派生、密文解密与授权尝试审计发生在普通账本写锁之外。最后短事务重查来源 active 状态/预期版本及受保护引用，拒绝确认后的更正、抑制或引用变化；本地显示与普通状态更改串行化。审计只记请求/对象/调用端ID、操作、结果和时间，不记主口令、原文或用途正文。`authorization_attempted` 仅表示已记录授权尝试，不证明人类看到了文字。

成功后最长展示30秒；窗口关闭、失去顶层焦点或输入桌面检查失败会清除控件并销毁请求。 helper定时检查 Windows input desktop，查询失败或非Default桌面时关闭；实现使用 [OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop) 和 [GetUserObjectInformationW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw)，关闭取得的句柄。

该桌面检查是保守护栏，**不等于已完成锁屏/断开会话联动验收**；官方API特别说明断开会话可能返回恢复连接后使用的桌面。尚无WTS事件联动、远程会话验收或安全桌面。Python内存释放不保证所有口令/私钥/明文副本清零；Tk窗口也不隔离同账户恶意程序或截图。

`evernook.vault_local` 只由本地CLI helper进程加载，普通MCP/查询进程不加载解密能力。内部 `LocalDisplayRequest` 为可信窗口和测试提供单次交付对象，不是经过认证的对外RPC接口；普通客户端无法通过查询参数获得它。目的地固定本地，没有 harness准入开关、IPC、跨进程授权token或字段级授予。因此本次不关闭完整T23，不启用日常凭证能力。

测试采用真实隐藏Tk控件和合成口令，不假称人类点击、可见界面或OS锁屏已验收。后续仍须验证实际窗口、WTS锁屏、harness注册/认证IPC、凭证换版/轮换及撤销后恢复。
