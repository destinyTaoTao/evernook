# 独立本机记忆服务 v1

首个实现对应 #107，总 spec #106。Windows 本机 NTFS，沿用现有 owner-only 目录与逐调用端 Access 权限。

首次启用独立服务须按下方切换章节使用管理员材料显式激活；已经激活的账本可在独立终端运行 `python -m evernook.service --config <仓库外 runtime.json>` 重启。未激活而直接启动返回 `service_activation_required`，不发布服务发现文件。进程固定服务一个账本，监听 127.0.0.1 随机端口。运行目录中的 memory-service.json 包含版本、端口及随机传输认证材料，只在 ACL 检查通过后加载。双方通过独立 nonce 的 HMAC-SHA256 挑战认证，客户端验证服务后才发送 Access 和正文。传输认证仅允许连接，操作还须通过原调用端身份与权限检查。传输仅支持本机，既有同账户恶意程序边界仍适用。

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

## #111 普通账本一次切换

Core0.1.15新增持久化service_mode。先停止切换前的旧宿主、旧CLI任务和其他持有原版运行时的进程，排空尚未确认的流与工具发布，再使用已配对admin身份：

```powershell
python -m evernook.service --config '<external runtime.json>' --activate-owner '<external mcp-access-owner.json>' --maintenance-access '<external mcp-access-background.json>'
```

服务先取得同账本单实例锁，再在跨进程独占切换栅栏和SQLite写事务内写入service_mode。普通新版本连接持有共享栅栏，连接开始后重新核对模式；授权后尚未开始事务的旧调用也无法跨越切换点写入。旧二进制不具备该栅栏，必须在切换前停止，其后续启动会因不认识新配置字段而拒绝。该机制防止旧入口误用，沿用同账户恶意代码可直接访问本机数据的边界。

激活之后，MCP及已支持CLI仅加载ACL检查的连接材料，不初始化本地SQLite。发现文件缺失、损坏或服务离线都失败关闭；重新启动服务可生成新发现材料。已生效标记不会因进程退出自动移除。切换后继续使用同一账本、原始回执、幂等密钥和来源/控制版本，新写后不得恢复旧快照或手工删除标记作为回滚。

服务模式支持前述可信CLI，加上remember/search/recall/read/state。新增service-pair以现有admin身份配对，默认只授capture/recall；service-pair-unlock提供管理员解锁。模型工具不开放配对或解锁。其他尚未服务化的本地管理命令明确拒绝，后续按对应功能票扩充接口；不能把普通客户端标成service_owner绕过服务。

普通切换暂不迁移保险库。发现已关联受保护原件时，激活返回protected_cutover_requires_migration且保留原配置、原件与授权查看能力。独立收件箱的pending对象无法仅凭普通账本发现，切换清单还须盘点仓库外收件箱和恢复材料；相应迁移在#115验收。不得因普通备份通过就宣称受保护资产已可迁移。

旧版开发／测试若有意保留双写路径，可在未激活账本上显式传入 `--allow-legacy-shared`；服务向 stderr 提示 `legacy_shared_mode_not_single_writer`，服务状态的 `service.single_writer` 为 false。该模式不会切换账本，不提供单写入者保证，不应用于日常数据。已存在受保护关联的账本拒绝以此模式启动；独立 pending 收件箱无法自动发现，不能据此认定切换安全。管理员激活和此标志不能同时使用。已激活账本的服务状态 `service.single_writer` 为 true，表示持久模式已设置且当前版本的入口受栅栏约束；此前已运行的旧二进制仍须按切换步骤停止，也不防同账户恶意代码。正常重启无需重复传入管理员材料。

Core 0.1.33（#151）在新建合成账本上复现旧无参数启动允许服务与旧运行时同时写入，随后增加默认拒绝。新增进程回归在旧代码失败；修复后服务契约9项、切换及桥接13项、后台维护及MCP28项通过。非 editable wheel 的切换6项及新增兼容／冲突2项通过，service 与切换测试 strict mypy 通过。首次切换测试因隔离测试环境缺少可选 PyNaCl 失败，安装项目固定版本后通过。未对现有用户账本做激活或迁移；#115 的私密输入、外部 pending 清点与迁移、#150 的完整处理仍未完成。
