# 独立本机记忆服务 v1

首个实现对应 #107，总 spec #106。Windows 本机 NTFS，沿用现有 owner-only 目录与逐调用端 Access 权限。

使用独立终端运行 `python -m evernook.service --config <仓库外 runtime.json>`。进程固定服务一个账本，监听 127.0.0.1 随机端口。运行目录中的 memory-service.json 包含版本、端口及随机传输认证材料，只在 ACL 检查通过后加载。双方通过独立 nonce 的 HMAC-SHA256 挑战认证，客户端验证服务后才发送 Access 和正文。传输认证仅允许连接，操作还须通过原调用端身份与权限检查。传输仅支持本机，既有同账户恶意程序边界仍适用。

JSON 使用四字节大端长度和 UTF-8，帧最多8MiB，拒绝重复字段和非有限 JSON 常量。握手及接收请求合计5秒，响应发送5秒；最多8个在途连接。操作本身沿用 Core 的协作时限，该首票尚无远程取消。任意客户端不可以请求修改绑定账本、运行程序或读取指定文件。异常只返回固定错误码，不输出请求或 traceback。

Python 客户端 `ServiceClient(config).call(access, operation, arguments, timeout=30)` 支持 remember/read/search/recall/status，参数映射已有 Core 操作。已提交但响应丢失时须按来源重投或按ID回查，网络异常不证明提交失败。截止时间或认证失败不回退直写库。

同账本启动使用 Windows 文件字节锁，第二实例拒绝。强杀后锁由系统释放，重新启动轮换传输材料，旧连接/旧发现文件无法继续认证。停止后留下的发现文件让客户端明确失败。当前服务基础不接管旧SDK与CLI，不应据此启用日常profile；统一桥接、后台生命周期、旧写入方隔离及正式一次切换分别由 #108–#111 验收。

验证为仓库外合成账本与真实独立子进程。#107 服务5项、启动竞争2项共7项通过，三个新增Python文件 strict mypy 通过。旧MCP/宿主/模型与规模未由本票重新验收。
