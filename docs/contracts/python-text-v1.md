# T05 本地 Python 文本边界

对应 [#6](https://github.com/destinyTaoTao/evernook/issues/6)，遵循 [R1 基线](r1-memory-v1.md)。实现是模块内调用契约；尚无 HTTP/MCP 服务、调用端配对 UI 或 dsh 生产适配。仅支持 Windows、本机固定 NTFS，Python `3.14.x`；当前实测 `3.14.4`。运行时无第三方依赖。

## 安装与调用

在仓库外创建 Python 3.14 虚拟环境，然后从 checkout 执行 `python -m pip install -e ".[dev]"`（使用该虚拟环境解释器）。验证命令为 `python -m unittest discover -s tests/contracts -v`、`python -m mypy`。所有样例只用合成内容。

```python
from pathlib import Path
from evernook import Event, MemoryRuntime, Source, initialize

# 父目录必须存在，data 目录必须尚不存在；这里只是合成试用目录。
config, access = initialize(Path(r"E:\evernook-lab\synthetic-text-data"),
                            client_id="synthetic-client", entry="synthetic-dsh")
memory = MemoryRuntime(config)
memory.handshake(access)  # 身份 + 契约版本检查，包含实际 SQLite 版本与未启用能力。
receipt = memory.submit(access, Event(
    Source("synthetic-dsh", "s1", "main", "u1"), "user", 1,
    "2021-02-03T10:20:30+08:00", "complete", "合成：37.50元。\r\n甲🙂e\u0301乙"))
saved = MemoryRuntime(config).read(access, receipt.message_id)
assert saved is not None and saved.event.text == "合成：37.50元。\r\n甲🙂e\u0301乙"
```

初始化通过 Windows 自带 `whoami/icacls` 移除新数据目录的继承权限并授予当前用户完全控制，之后才写配置、访问 token、幂等密钥和数据库。已有目录拒绝覆盖。每次启动用系统 Windows PowerShell 的 .NET ACL 接口核验目录和运行文件没有其他身份授权；失败拒绝启动，不自动修改已有目录权限。`runtime.json` 严格 schema v1，`client.json` 新建为 v2、兼容旧 v1；未知字段、重复 JSON 键、错误类型/版本、checkout 内、相对路径及非本机固定 NTFS 路径均拒绝。当前数据库 `user_version=4`，兼容升级旧 v1–v3，见[工具与附件](capture-v1.md)。凭证和 SQLite/WAL/SHM 不得通过链接跳出数据目录。令牌不进入 CLI 参数或环境变量。

重启时，受信任的本机宿主从目录内 `client.json` 装载 `client_id`、`token` 创建 `Access`，不得记录文件内容。当前每个数据目录只支持一个受信任调用端身份和一个绑定的 `entry`；所有操作都要求 token 和 `contract_version=1`。同用户进程可读取授权文件，本边界不隔离已经控制该 Windows 账户的进程，也不提供身份分发服务。未来传输适配器必须复用入口检查。

## 保存、读取与失败

- `submit(access, event)`：来源四元组为幂等键，整个请求内容（含角色、时间、序号和安全标记）核对一致才返回原回执；同键改变完成状态也会冲突。流片段/最终消息须分配独立源事件，不能用覆盖更新模拟原件。
- `read`、`read_source` 返回 `StoredMessage | None`，包含原始事件和稳定回执；`list_messages(since, until, limit, offset)` 返回 `Page`。时间范围为源事件时刻 `[since, until)`，缺失源时间的消息只进入未限定时间的列表。原时间字符串与偏移保持不变，接收时间为 UTC。
- T07 后，普通读接口过滤被更正/撤销/抑制目标；`state` 查询当前状态，获单独授权的 `review_archive` 才读取旧原件与变更记录。保存回执描述历史提交，不意味着目标当前允许召回。
- 列表按源时间、入口/会话/分支、源序号、消息 ID 排序；未知时间排在前面。`limit` 为 1–1000，超出通过 `next_offset` 继续。offset 是当前视图分页，并发新增可能移动位置；需要固定导出快照的调用端须等待后续导出契约，不能把本分页当快照。
- 普通成功为 `committed / original / original_saved=true`；SQLite WAL + `synchronous=FULL`，原文与一个待处理任务在同一短事务中提交后才返回。任务目前仅为账本中的待处理记录，没有运行消费者、模型或外部操作。
- 保留 Unicode code point、不做归一化；严格 UTF-8，单条文本最大 1 MiB UTF-8 字节。显式保存源时间未知和 `partial/complete/canceled/failed`。不补造完整流、不自动拼接重试。
- `MemoryFailure.code` 是不含输入内容的固定错误码：如 `unauthorized`、`incompatible_contract`、`unauthorized_source`、`source_conflict`、`invalid_encoding`、`storage_busy`、`storage_full`、`storage_unavailable`。未知异常不能视为保存成功；中断后用原来源/内容重试确认。

## 敏感路径与未启用能力

显式 `sensitive`、`protected_source` 或 v1 凭证信号（中英文密码/令牌/密钥赋值、Bearer、私钥头、若干 token 前缀、URL 用户密码）触发**整条文本**替换为 `[PROTECTED:ORIGINAL_NOT_SAVED]`，不尝试保留周围上下文。回执为 `projection_only / projection / original_saved=false`，安全投影也可靠提交后才确认。来源身份使用非敏感、不含业务原文的稳定标识；已知凭证信号出现在标识中会拒绝为 `sensitive_metadata`。

检测是有边界的启发式，无法识别所有自然语言、编码/拆分秘密；调用端应显式标记受保护来源。数据在检测前只用于内存中的输入校验；核心没有请求日志、模型输入或网络调用。幂等指纹使用独立受目录 ACL 保护的随机 HMAC 密钥，普通数据库中没有原始秘密或无密钥的低熵散列。该密钥丢失则拒绝启动，不能自动生成新密钥破坏重试核对。

保险库关闭；没有受保护原件可恢复，也不能将投影回执显示成原件已保存。显示前屏障及分块安全检测由 T10 集成验收，本接口不授权先展示敏感原文。备份目的地、启用与恢复验收按用户决定待办，`handshake/status` 保持未启用。数据目录 ACL 不是数据静态加密；本票据不接受日常长期运行、RPO/RTO、物理掉电或全盘故障保证。
