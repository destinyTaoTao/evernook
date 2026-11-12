# 独立本机记忆服务 v1

首个实现对应 #107，总 spec #106。Windows 本机 NTFS，沿用现有 owner-only 目录与逐调用端 Access 权限。

使用独立终端运行 `python -m evernook.service --config <仓库外 runtime.json>`。进程固定服务一个账本，监听 127.0.0.1 随机端口。运行目录中的 memory-service.json 包含版本、端口及随机传输认证材料，只在 ACL 检查通过后加载。双方通过独立 nonce 的 HMAC-SHA256 挑战认证，客户端验证服务后才发送 Access 和正文。传输认证仅允许连接，操作还须通过原调用端身份与权限检查。传输仅支持本机，既有同账户恶意程序边界仍适用。

JSON 使用四字节大端长度和 UTF-8，帧最多8MiB，拒绝重复字段和非有限 JSON 常量。握手及接收请求合计5秒，响应发送5秒；最多8个在途连接。操作本身沿用 Core 的协作时限，该首票尚无远程取消。任意客户端不可以请求修改绑定账本、运行程序或读取指定文件。异常只返回固定错误码，不输出请求或 traceback。

Python 客户端 `ServiceClient(config).call(access, operation, arguments, timeout=30)` 支持 remember/read/search/recall/status，参数映射已有 Core 操作。已提交但响应丢失时须按来源重投或按ID回查，网络异常不证明提交失败。截止时间或认证失败不回退直写库。

同账本启动使用 Windows 文件字节锁，第二实例拒绝。强杀后锁由系统释放，重新启动轮换传输材料，旧连接/旧发现文件无法继续认证。停止后留下的发现文件让客户端明确失败。

验证为仓库外合成账本与真实独立子进程。#107 服务5项、启动竞争2项共7项通过，三个新增Python文件 strict mypy 通过。旧MCP/宿主/模型与规模未由本票重新验收。

## #108 桥接增量

stdio MCP 检测到服务发现文件后，通过服务执行既有参数模型与操作；工具列表从服务取得。服务离线仍保留发现文件，桥接明确失败。服务可显式启动 --context-config/--embedding-config，宿主若指定这些配置，必须与服务绑定路径匹配。普通调用不能指定新模型路径或策略。较长MCP操作的传输等待跟随合法执行预算并保留10秒余量，context取消经连接关闭传递至Core与tokenizer。

可信 CLI 的 manage、host-recovery、mark-important、change、purge-preview/purge、profile-read/profile-decide、candidate-list、fact-query/fact-decide、scheduler-trigger/status/session-end 和 status 通过同一服务执行，沿用逐次权限与来源/预览版本。其他旧CLI入口与直接Python调用的隔离留至#111。服务模式尚未构成日常切换。

验证包括原CLI/MCP/服务36项，以及经服务重跑的原20项MCP契约。新增桥接5项与服务5项共10项通过，涵盖真实stdio与可信CLI、失败关闭、身份权限、断连取消、真实本地tokenizer输出及挂起worker取消回收。预算回归先在缺少传输预算时失败，再修复通过。所有相关6个文件strict mypy通过。尚未验收独立安装dsh或正式模型。
