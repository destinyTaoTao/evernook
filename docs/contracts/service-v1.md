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

## #109 服务维护生命周期

显式提供 --maintenance-access 启用维护，要求独立可信身份具备 recall/maintain/accept。--maintenance-interval 为0.2至3600秒，默认5秒。--navigation-policy 可重复传入至多16个已保存策略；服务不自行创造导航策略或准入处理器。

服务周期读取原持久化 scheduler 设置。全局暂停及前台活动阻止本轮后台；运行途中按单条词法来源、导航协作信号与阶段边界重新检查。停止不打断已开始提交的事务。每轮词法至多16条且软预算1秒，导航每策略至多16来源/1任务/0.5秒，沉淀沿用保存的来源、轮次及时间预算。未准入的自动发布保持原状态。该增量未启用语义模型的周期索引。

status和MCP status提供service.independent与maintenance，显示tick数、错误码、跳过原因及最后一轮词法/导航/沉淀状态。实时积压仍可通过原scheduler-status读取。关闭全部客户端不停止后台，重启复用已保存进度和租约；正常关闭通知协作任务并等待退出。

服务维护、原scheduler、原navigation共33项通过。加入真实导航策略后服务维护3项再次通过，4文件strict mypy通过。独立评审发现的运行中暂停缺口已补动态信号及回归。首次命令误写不存在的test_navigation模块，改为项目实际test_maintenance后通过，原失败输出保留。尚未验收物理重启或正式模型/规模。
