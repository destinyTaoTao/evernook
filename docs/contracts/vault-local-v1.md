# T23 单次本地查看增量

对应 [#24](https://github.com/destinyTaoTao/evernook/issues/24)，在 [受保护收件箱](protected-inbox-v1.md) 之上提供显式启动的本地 Tk 窗口。它不是模型工具、持续解锁会话或 harness 明文交付协议。以下直接访问命令适用于服务切换前；切换后使用该契约中 Core 0.1.43 的服务持有窗口入口。

```text
<python> -m evernook.cli vault-show --config <runtime.json> --access <access.json> --inbox-dir <inbox> --key-dir <keys> --object-id <UUID> --purpose <本次非敏感用途>
```

主口令没有参数/环境变量/stdin路径，只能输入该窗口的隐藏 Entry 控件。窗口固定显示调用端、对象、版本、操作、用途和本地目的地，用户点击“确认本次用途并仅在本地展示”后，才进行本次验证和解密；不接受 `approved=true` 或模型声称用户同意。调用端使用已配对访问文件，沿用收件来源入口限制。

每次启动创建一个绑定对象、密钥/来源版本和精确签名信封的请求，有效期120秒。错误口令、撤销、取消、过期、解密或交付失败均消费本次请求；线程内并发重试最多一次交付。审计为尽力而为：审计存储不可用不掩盖真实拒绝码（如 `vault_unlock_refused`/`vault_source_unavailable`）；Python 构建缺少 tkinter 时 CLI 报 `vault_display_unavailable`。解锁不授予全库访问，也不留下通用明文读取令牌。明文只进入本地 Label，不写 stdout、剪贴板、账本、FTS或审计；CLI结束只返回请求ID和窗口关闭状态。

ACL检查、主口令派生、密文解密与授权尝试审计发生在普通账本写锁之外。最后短事务重查来源 active 状态/预期版本及受保护引用，拒绝确认后的更正、抑制或引用变化；本地显示与普通状态更改串行化。审计只记请求/对象/调用端ID、操作、结果和时间，不记主口令、原文或用途正文。`authorization_attempted` 仅表示已记录授权尝试，不证明人类看到了文字。

成功后最长展示30秒；窗口关闭、失去顶层焦点或输入桌面检查失败会清除控件并销毁请求。 helper定时检查 Windows input desktop，查询失败或非Default桌面时关闭；实现使用 [OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop) 和 [GetUserObjectInformationW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw)，关闭取得的句柄。

Core 0.1.80 的桌面检查还查询当前进程所属 Windows 会话，只接受 WTS 活动且解锁状态；查询失败、会话不匹配、断连、未知状态和非 64 位进程均拒绝。Windows 7／Server 2008 R2 的锁定标志存在反转，当前实现对 Windows 8 之前的版本拒绝而不猜测。状态语义见 [Microsoft WTSINFOEX 文档](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)。

该检查是轮询护栏，**不等于已完成锁屏/断开会话联动验收**：状态检查和提交／展示不是原子的，事件循环阻塞会延后清空，短暂锁定后解锁也可能错过；现无 WTS 事件联动、远程会话验收或安全桌面。Python 内存释放不保证所有口令/私钥/明文副本清零；Tk 窗口也不隔离同账户恶意程序或截图。

Core 0.1.80（#221）最终源码和非 editable wheel 各通过 31 项合成回归，包含 WTS 状态／短缓冲／API 失败和释放、32 位拒绝、输入前及隐藏窗口清空、既有单次查看与服务保存；相关 5 个文件 strict mypy 通过。独立审查复现轮询重复加载 DLL 的引用计数累积，已分别缓存成功加载的系统库并覆盖后续初始化失败的重试。当前环境做了不改变状态的只读会话探测，审查核对了官方标志语义；没有锁定用户电脑，也没有验证真实锁屏、断电或物理本人身份。

直接访问模式中，`LocalDisplayRequest` 为可信窗口和测试提供单次交付对象；服务模式中，由独立服务持有该对象并通过匿名管道向其本机窗口子进程交接。受认证、加密的服务请求只启动本地窗口并返回关闭回执，普通查询和 MCP 不交付原文。两种模式的目的地均固定本地，没有 harness 明文授权或字段级授予，因此不关闭完整 T23，也不启用日常凭证能力。

测试采用真实隐藏Tk控件和合成口令，不假称人类点击、可见界面或OS锁屏已验收。后续仍须验证实际窗口、WTS锁屏、harness注册/认证IPC、凭证换版/轮换及撤销后恢复。
