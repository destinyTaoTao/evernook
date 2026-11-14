# T22 受保护收件箱 v1：显式加密收件增量

对应 [#23](https://github.com/destinyTaoTao/evernook/issues/23)，实现为 `evernook.protected`。安装 `.[vault]`，固定 PyNaCl 1.6.2；默认普通运行时不导入、不启用此模块。初始化只供可信本地操作端使用，不是 MCP 工具。

```python
from pathlib import Path
from evernook import MemoryRuntime
from evernook.protected import initialize_protected_inbox, ProtectedInbox

# 路径均为占位；password 只能来自可信本地隐藏输入，不使用 argv/env/chat。
runtime = MemoryRuntime(Path("E:/configured-memory/runtime.json"))
access = runtime.access_from_file(Path("E:/configured-memory/mcp-access-owner.json"))
inbox = initialize_protected_inbox(runtime, Path("E:/configured-protected-inbox"),
                                 Path("E:/configured-vault-material"), password)
result = inbox.submit(access, source_bound_event)
assert result.state == "protected_linked"
assert result.encrypted_original_saved
assert result.message is not None and not result.message.original_saved
```

收件目录、密钥材料目录和普通运行目录必须是三个分离的仓库外私有目录；初始化拒绝已有目录。接收公钥、发送签名公钥及固定格式生成 key ID；Ed25519 私密签名材料可供写入端使用，接收私钥只以 Argon2id interactive 派生密钥及 SecretBox 加密保存。收件端加载公钥/签名材料，不加载或解密 recovery.json。公钥加密使用成熟 [SealedBox API](https://pynacl.readthedocs.io/en/latest/public/)，签名另行绑定发送者及对象；[口令派生](https://pynacl.readthedocs.io/en/latest/password_hashing/)不自行设计算法。

## 来源、事务与回执

1. `stage(access,event)` 验证核心身份和来源，检查 UTF-8/字段/1 MiB 正文上限，使用独立域的 HMAC 指纹进行幂等；没有公开低熵明文散列。原始 Event 加密，密文及签名绑定 inbox/object/key/version、脱敏投影、来源及指纹。独立 SQLite FULL/WAL 事务提交后只返回 `protected_pending_link`，消息回执为空。
2. `reconcile(access,object_id)` 核验固定接收材料、签名和对象绑定；将投影与受保护引用写入普通账本的同一事务，复用原有来源唯一性和任务生成。普通 `Receipt` 的 `original_saved=false,representation=projection` 保持原意；不能拿它声称明文已经进入普通账本。
3. 核心提交后才更新收件箱为 `protected_linked`。只有外层 `encrypted_original_saved=true,state=protected_linked` 表示两个存储已关联；这是密文保存确认，不授予解密/交付权限。状态读取重新核对核心引用，关联缺失时拒绝伪造成功，显式 reconcile 可修复缺失的引用。

两个库不伪装成跨库原子事务。任何窗口中断后，使用 `pending()`（每页最多100，按完成移除）和稳定 object ID 重试；核心已提交但最终确认缺失时仍可重试，不重复创建消息。改变原始内容但复用来源返回 `source_conflict`；更正/抑制不删除密文或自动恢复旧可见状态。

普通账本只保存 `[PROTECTED:ENCRYPTED_ORIGINAL]`、非敏感来源元数据、受保护引用及 HMAC。普通读取/词法索引拿不到原件；该模块没有解密方法或普通模型取密工具。初始化或加密失败明确报错，禁止回退明文；未完成初始化的目录不会被自动当作有效保险库。

## CLI 与未启用范围

- `vault-init --config ... --access ... --inbox-dir ... --key-dir ...`：本地 TTY 隐藏输入并二次确认口令；管道、参数或环境变量不能提供口令。
- `vault-receive` 使用同样的四个路径选项，UTF-8 stdin 接受完整 Event；只输出脱敏回执。此 stdin 传输是可信本地采集接口，不是主口令输入途径。
- `vault-pending` 列出本入口的待关联对象，`vault-reconcile --object-id ...` 显式补齐关联。普通 CLI 不新增读取明文命令。
- `vault-inventory --config ... --access ... --inbox-dir ... --key-dir ... [--limit 1..100] [--after <cursor>]`：切换前由管理员明确指定一个收件箱，逐页检查 pending 与 linked 对象。Python 可调用 `ProtectedInbox.inventory(access, limit=100, after=None)`；返回已验证对象的脱敏回执和下一页 cursor。只校验信封签名、绑定及 linked 账本引用，不载入口令或解密；任一页对象或关联损坏时整页失败。普通身份不可清点其他入口。返回前重新核对管理员权限。
- `vault-links` 使用相同路径、limit 和 cursor，或调用 `ProtectedInbox.inventory_links(...)`，从普通账本反向列出受保护引用。当前收件箱的签名、原始投影、指纹、回执和引用一致时标记 `linked_verified`；核心已提交但收件箱尚未确认时标记 `pending_acknowledgment`；其他收件箱标记 `foreign_unverified`，不能当成验证通过。当前收件箱的对象缺失或损坏、引用不一致时整页拒绝。

上述旧版本地清点命令用于账本切换前；独立服务已激活后，旧的直接访问会被拒绝。Core 0.1.42 另提供下述服务接口；两者都不是模型工具。`linked_verified` 只表示当次检查的结构和关联一致，不表示对象可解密、仍被授权展示，或其他收件箱已验证。

### 激活后的管理员清点

Core 0.1.42 的已激活独立服务可对启动时绑定的收件箱执行同样的元数据清点。可信管理员使用 Python `ServiceClient.protected_inventory(access, after=None, limit=100)` 和 `protected_links(...)`，或分别调用 `service-protected-inventory` / `service-protected-links`，参数和已有管理 CLI 一样通过 stdin JSON 提供，例如 `{}` 或 `{"limit": 10, "after": "<上页游标>"}`。请求及回执使用经双方认证的加密通道；没有 vault 依赖、绑定收件箱或管理员权限时拒绝，不回退明文。MCP 和模型工具不暴露这两项操作。

清点返回脱敏回执、对象／账本 ID、关联状态与分页游标；它不载入口令、不解密原件，也不证明本人的身份、对象可授权交付或迁移完成。未配置的其他收件箱不会自动发现，跨库清点仍不是原子快照；文件损坏或当前收件箱关联缺失时拒绝相应页。应通过可信本地终端或 SDK 使用，勿将这些元数据当作普通聊天内容。

### 激活后的单次本机查看

Core 0.1.43 的 Windows 独立服务支持 `ServiceClient.protected_show_local(access, object_id, purpose)` 或 `service-protected-show-local --config ... --access ... --object-id ... --purpose ...`。`purpose` 是非秘密的查看用途，不应包含口令或私密正文。请求必须属于原记录的配对入口并具有 recall 权限；服务检查对象及来源控制，启动自己拥有的本机窗口，由本人在窗口输入保险库口令。调用方只收到请求编号、窗口目标和关闭状态，关闭状态不证明成功解锁或看到内容；服务 API、CLI、MCP 和宿主不接收原文或口令。一次只允许一个窗口，取消、超时、权限变更或验证失败时关闭或拒绝，不回退普通读取。

服务和原生窗口子进程通过继承的本机匿名管道交换口令和原文，内容也会短暂存在双方进程内存。授权事务内只完成内存交接，管道写入在事务结束后进行：授权交接之后的撤销不能撤回已经交付的数据。窗口失焦或关闭时清除显示；真实锁屏、截图、输入法、同账户恶意进程和安全内存擦除不在已验证保证内。口令是知识因素，并不证明物理本人身份；接口不提供模型或宿主的秘密交付。旧保险库迁移及完整身份认证仍未完成。

Core 0.1.43（#166）在隔离的安装版服务与 Windows 窗口上，用合成对象和口令完成查看：原文显示于窗口，关闭后 CLI 仅收到非秘密回执；停止服务后扫描运行目录的 29 个文件，没有发现合成原文或口令。源码本地查看 9 项回归与 strict mypy 通过；服务回归覆盖其他入口、未配置收件箱和明文传输拒绝。测试目录中的合成夹具脚本本身包含合成口令，因此不计入上述运行目录扫描。未操作用户保险库或验证真实锁屏。

### 显式服务端加密收件

可信本地操作者可在首次服务激活前创建空收件箱与密钥目录，并在启动服务时同时指定 `--protected-inbox <inbox-dir> --protected-keys <key-dir>`。两项必须同时配置，不能用于旧共享模式；绑定身份或文件无效时服务拒绝启动。首次激活在独占切换栅栏内拒绝该收件箱中任何旧对象，旧原件与 pending 的完整迁移仍由 #115 处理。重启服务时须再次提供这两个路径；遗漏时私密收件明确拒绝，不回退到普通写入。账本外仍可能有未指定的独立收件箱，不能据此认定全局迁移安全。

可信本地调用方可使用 `ServiceClient.protected_receive(access, event)` 或 `service-protected-receive --config ... --access ...`（完整 Event JSON 通过 UTF-8 stdin），Event 必须 `sensitive=true` 且 `completion=complete`，来源入口须匹配已配对的 capture 身份。仅正文加密，来源等元数据仍可见，调用方必须使用不含私密正文的来源标识；未配置、权限不足、非完整事件、加密或存储失败均不明文回退。普通读取、搜索与维护只接触投影；此操作不是 MCP 模型工具，也不授予解密或披露权限。中断或响应丢失不证明未提交，应以同一来源与相同事件重试以恢复回执。

`service-private-window --config ... --access ... --entry <配对入口>` 可打开独立的本地私密记录窗口，正文只在窗口输入，直接调用加密收件，不进入 dsh 聊天或模型。保存失败后窗口仅允许以同一内容和来源编号重试；关闭时 CLI 只返回 `saved`、`canceled` 或 `outcome_unknown` 及非秘密来源编号。结果不确定时，可使用返回的 `--session-id` 和 `--event-id` 重新打开，输入完全相同的内容重试；若进程被强杀且未保存窗口显示的编号，则可能无法避免新建重复记录。窗口失焦后短暂复核并清空关闭，也检查输入桌面，但不等于真实 OS 锁屏、截图、输入法或同账户进程隔离验收。Python/Tk 字符串及内存无法保证安全擦除。该窗口不是私密对话，不能让模型使用秘密，也不替代逐次本人认证披露。

Core 0.1.38（#156）在源码与独立 wheel 分别通过本地状态机、真实 Tk 控件及服务集成测试；CLI 与服务相关12项、strict mypy通过。实际 Windows 窗口在合成账本上输入并加密保存，安装版与源码版均显示保存成功并清空输入，停止服务后两条对象已关联，所有可读测试文件未发现合成正文。初次实际自动化因瞬时焦点切换关闭窗口，加入短暂焦点复核并测试窗口内切换与真正切出后复测；第一次文件扫描因运行中的锁文件不可读，服务停止后完成全文件扫描。此验证没有模拟 OS 锁屏或使用真实用户秘密，也没有将窗口接入 dsh 聊天。

服务 status 的 `service.protected_capture` 仅表示启动时绑定了收件箱，并非后续文件完整性、写入健康、身份认证披露或日用准入证明。每次写入仍重新检查必要的文件与权限。

Core 0.1.36 的回环传输本身未加密。Core 0.1.37 使用既有本机回环 HMAC 双向认证的随机挑战与端点密钥派生单连接密钥，再使用 [PyNaCl 1.6.2 SecretBox](https://pynacl.readthedocs.io/en/latest/secret/) 对私密请求和回执分别加密、认证；请求中包含的访问凭证也在密文内。新客户端遇到旧服务或缺少加密依赖时不会回退明文；新服务拒绝明文私密请求。**客户端和服务必须一起升级**：旧客户端会在收到拒绝前发送明文，新服务无法撤回已传输的数据。普通服务请求仍使用原协议，不能声称所有本地流量都加密。

帧长度、连接时间及私密帧标志仍可观察；该方案没有前向保密，端点密钥泄露后可能解密同一服务实例已录制的流量。发送端和服务进程内存中仍短暂存在原文，端点密钥受既有本地目录 ACL 保护；同一账户恶意进程、具备足够权限的本机进程及内存／交换文件不在该能力的隔离保证内。调用者必须在宿主转录、日志或模型处理之前由可信入口显式分流；此接口本身不实现 dsh 私密会话、流/附件加密或按人认证披露，不能用于声称 #115 已完成。

Core 0.1.36（#154）使用合成数据的源码私密服务8项、旧收件箱／本地查看28项、切换／CLI19项通过；独立 wheel 的服务7项、更新后的收件与错误材料补充验证以及 strict mypy 通过。检查包含本地 CLI、已配对权限与入口、明确标记、启动材料错误、切换竞争、已提交密文但关联失败后的重启重试，及合成原话／密码不出现在普通库文件和诊断；测试中使用合成恢复材料单独解密核对原文。未配置服务的新增断言先在旧代码失败。首轮测试清理由未关闭的测试 SQLite 连接阻塞，关闭测试连接后通过；权限审计新增连接使旧崩溃注入钩子提前触发，已改为确认目标关联提交后退出并完整复测。未操作用户保险库，也未验证 OS 网络隔离或 dsh 私密输入。

Core 0.1.37（#155）对合成私密事件验证双向帧不含正文或访问令牌，明文请求、篡改、跨连接重放和请求／响应方向互换均拒绝；客户端缺少加密依赖或加密出错时不发送私密帧、不回退。源码私密服务12项、补充故障测试3项、普通服务和桥接16项通过；最终非 editable wheel 私密服务16项、方向补充1项通过，实际 0.1.36／0.1.37 双向组合以及不装 vault extra 的客户端均参与验证；无 vault extra 的独立安装普通服务9项和 strict mypy 通过。首次明文拒绝测试在旧实现中按预期失败。未使用真实用户数据，也未宣称密钥泄露后的流量安全或 dsh 私密入口已完成。

清点不枚举其他目录、未知或缺失的收件箱，也不验证其他收件箱的对象；分页及两个数据库不是原子快照，必须停止写入并按完整迁移流程复核。签名有效不证明持有恢复材料或可以解密，回执不能作为启动服务、销毁或迁移完成的授权。

Core 0.1.34（#152）在隔离合成库完成源码受保护收件箱及 CLI 24 项测试，非 editable wheel 上新增清点回归5项及 strict mypy 通过。首个新增回归在未实现接口时按预期失败；另以合成环境验证，即使恢复文件缺失，签名清点仍可成功，因此不能将它误当作可解密证明。清点会写入普通授权审计，不更改密文或关联；本次未访问用户保险库。

Core 0.1.35（#153）增加反向清点：源码受保护收件箱及 CLI 30 项通过，独立 wheel 的新增反向检查5项和权限／分页补充2项通过，strict mypy 通过。测试包括真实子进程在核心提交后退出、缺失和篡改对象、损坏投影及回执、其他收件箱未验证和管理员权限复核。首轮负向测试先因接口尚未实现而失败；另一轮因合成夹具重复配对同一入口失败，改用独立入口后通过。未操作用户保险库，未完成整体迁移、解密验证或日用准入。

路径必须显式配置，未改现有 runtime.json 或日常宿主行为。`MemoryRuntime.submit/submit_capture`、dsh/pi 的敏感自动分流尚未接入此可选收件箱；原有普通路径仍然拒存敏感原件。当前加密收件仅覆盖 Event 正文，未将原始流或附件加密归档，不能声称 T22 全部验收。

独立收件箱与密钥目录尚未纳入普通备份；**普通归档和备份只有投影/引用，不能恢复受保护原件**。已有显式 [单次本地查看 helper](vault-local-v1.md) 增量；轮换、密钥与密文成套备份、最新撤销重放、完整锁屏/harness授权交付及T24治理仍待后续实现和验收。恢复材料遗失不提供绕过。

同一 Windows 账户的恶意进程、内存/交换文件以及宿主先行留档不因目录 ACL 或 Python 对象释放而消失；未宣称抵御这些边界，也未启用生产凭证能力。
