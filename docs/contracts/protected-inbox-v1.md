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

路径必须显式配置，未改现有 runtime.json 或日常宿主行为。`MemoryRuntime.submit/submit_capture`、dsh/pi 的敏感自动分流尚未接入此可选收件箱；原有普通路径仍然拒存敏感原件。当前加密收件仅覆盖 Event 正文，未将原始流或附件加密归档，不能声称 T22 全部验收。

独立收件箱与密钥目录尚未纳入普通备份；**普通归档和备份只有投影/引用，不能恢复受保护原件**。已有显式 [单次本地查看 helper](vault-local-v1.md) 增量；轮换、密钥与密文成套备份、最新撤销重放、完整锁屏/harness授权交付及T24治理仍待后续实现和验收。恢复材料遗失不提供绕过。

同一 Windows 账户的恶意进程、内存/交换文件以及宿主先行留档不因目录 ACL 或 Python 对象释放而消失；未宣称抵御这些边界，也未启用生产凭证能力。
