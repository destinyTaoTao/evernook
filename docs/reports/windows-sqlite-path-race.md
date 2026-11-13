# Windows SQLite日志生命周期与启动校验

2026-11-08，实施[#103](https://github.com/destinyTaoTao/evernook/issues/103)。#101真实安装的dsh两个短pilot未通过，第二次明确返回`unsafe_runtime_path`；失败证据保留，未修改正式1800秒、2GiB内存或10GiB磁盘门槛。

## 已确认原因与最小修复

并发SQLite连接关闭时，`memory.sqlite3-shm`会消失。Windows `Path.resolve()`偶发返回`\\?\C:\...`，同一root却是`C:\...`，父目录比较把同一位置误报为越界。Core0.1.2仅将解析后的本机扩展盘符路径归一到普通盘符形式，然后继续执行checkout排除、本地固定NTFS和父目录检查；不把UNC/device路径转换成本地路径。回归还证明此规范化避免了扩展盘符别名绕过checkout排除。

另一处实际诊断捕获到`File.Exists`之后、`File.GetAccessControl`之前SHM被删除，PowerShell包装异常的根异常为`System.IO.FileNotFoundException`。仅当当前检查项是运行库根下的`memory.sqlite3-wal/shm`且根异常精确匹配时返回专用重查状态；Python最多重新执行三次完整ACL校验。其他文件、真实权限错误、超时或重查耗尽仍拒绝，不修复现有ACL，不吞掉通用错误。

## 验证

- 六项路径回归通过：同一盘符身份、真实Runtime接受可选日志、checkout排除、外指凭证及UNC拒绝、真实PowerShell删除日志后完整重查、真实错误及耗尽失败关闭。路径/既有边界共11项通过。
- MCP全20项通过；Core和新增探针strict mypy通过（44个Python源文件）。未运行全部业务套件。首次直接按模块运行MCP因测试辅助模块搜索路径不完整而未加载，随后按仓库`unittest discover -s tests/contracts -p test_mcp.py`成功运行。
- 源码并发探针100次Runtime启动、零错误，拥有的后台SQLite写入2258次，约30.01秒。
- 新独立Core0.1.2 wheel安装到非editable环境，与源码及wheel逐一核对45个包内Python/Node文件，全部字节相同。实际安装版再运行100次启动、零错误，后台写入4090次，约31.89秒；记录的import路径位于该独立site-packages。

wheel SHA-256：`38f47444b3491238f7f068bf1f36c1b46d4a7fca27c4b9680a973a529ed0f39a`。版本与哈希同时识别产物；不原地覆盖旧环境。

可重复探针：`probes/installed/path_race.py --output <fresh external directory> --iterations 100`，使用指定安装版Python运行。它仅在新合成运行库上创建计数表，后台子进程循环提交并关闭连接；逐次验证子进程存活，核对实际写入量并限时停止。运行数据、权限材料和原始诊断均保存在仓库外。

这些验证覆盖启动路径与权限竞争，不代表dsh长跑、真实模型、物理睡眠/断电或日用已通过。#101将使用新安装组合重新跑pilot，保留旧失败记录；#81/#82/#94的剩余准入仍开放。
