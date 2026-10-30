# 本地 CLI v1

对应 [Issue #30](https://github.com/destinyTaoTao/evernook/issues/30)。CLI 是可信本机操作端，复用 [Python 核心契约](python-text-v1.md)，不增加数据库、网络服务、保险库或自动采集/注入。仍只支持 Python 3.14、Windows 本机固定 NTFS。

## 安装与初始化

在 checkout 外创建虚拟环境，从 checkout 安装 `python -m pip install -e .`。核心与 CLI 均无第三方运行依赖；需要 MCP 时安装 `.[mcp]`，完整回归安装 `.[dev]`。以下命令中的 `python` 和 `evernook` 均指该虚拟环境的解释器和入口。也可以用 `python -m evernook.cli` 替代 `evernook`。

仅为合成示例，不会因阅读文档创建运行目录：

```powershell
evernook init --data-dir E:\evernook-lab\synthetic-cli-data --client-id synthetic-owner --entry synthetic-native
evernook pair --config E:\evernook-lab\synthetic-cli-data\runtime.json --client-id synthetic-mcp --entry synthetic-mcp --access E:\evernook-lab\synthetic-cli-data\mcp-access-synthetic.json
```

初始化要求父目录存在、目标数据目录尚不存在。它先收紧目录 ACL，再初始化账本，输出 `config` 和 `access_file` 路径；owner 访问文件名为 `mcp-access-owner.json`。不输出 token。初始化中途失败可能留下部分目录，应由本地维护者检查，不能删除后重试真实数据目录。配对沿用核心的冲突拒绝和幂等重试，不轮换已有身份。访问文件只在同一私有运行目录内使用；`client.json` 是身份登记表，不是 CLI 的 `--access` 文件。

## 输入与结果

| 命令 | 输入 | 结果 |
|---|---|---|
| `init` | `--data-dir --client-id --entry` | 新配置与 owner 访问文件路径 |
| `pair` | `--config --client-id --entry --access` | 配对访问文件路径 |
| `remember` | `--config --access`；stdin 为完整 Event JSON | 持久化 Receipt，包括 `original_saved` |
| `search` | `--config --access`；stdin 为搜索参数 JSON | 带来源/坐标与覆盖状态的 SearchPage |
| `read` / `state` | `--config --access --message-id` | 当前允许的原文/投影或状态版本 |
| `status` | `--config --access` | 核心能力、账本数量、词法水位、消费者未运行状态 |
| `refresh-index` / `rebuild-index` | `--config --access` | 一批最多 100 条的词法维护结果 |
| `recall` | `--config --access`；可选 `--embedding-config`；stdin 查询 JSON | [词法/语义融合与自适应近期证据](semantic-v1.md) |
| `refresh-semantic` / `semantic-status` | `--config --access`；可选 `--embedding-config` | 当前模型空间的一批最多 16 个片段维护或覆盖状态 |

`rebuild-index` 先重置派生索引，再处理首批；`pending_messages > 0` 时继续运行 `refresh-index`，不要反复执行重建。维护成功返回不等于全库已经追平，必须读取 `state/pending_messages`。操作期间原文仍遵守既有提交与过滤语义。

保存与查询正文不接受命令行参数，使用 **严格 UTF-8 的 stdin 单个 JSON 对象并以 EOF 结束**；拒绝重复键、未知参数、错误类型和超过 8 MiB 的线上输入。核心仍限制单条文本最多 1 MiB UTF-8；线上上限为 JSON 转义预留空间，不放宽核心限制。stdin 不做换行归一化；原话中的换行、emoji、组合字符按 JSON 内容原样保存。

例如在仓库外创建仅含合成数据的 `E:\evernook-lab\synthetic-event.json`：

```json
{"source":{"entry":"synthetic-native","session":"s1","branch":"main","event":"u1"},"role":"user","sequence":1,"occurred_at":"2021-02-03T10:20:30+08:00","completion":"complete","text":"合成：乌龙茶37.50元。\r\n甲🙂é乙","sensitive":false,"protected_source":false}
```

PowerShell 下用字节管道避免旧版 PowerShell 文本管道改变编码：

```powershell
python -c "import pathlib,subprocess,sys; subprocess.run([sys.executable,'-m','evernook.cli','remember','--config',r'E:\evernook-lab\synthetic-cli-data\runtime.json','--access',r'E:\evernook-lab\synthetic-cli-data\mcp-access-owner.json'],input=pathlib.Path(r'E:\evernook-lab\synthetic-event.json').read_bytes(),check=True)"
evernook status --config E:\evernook-lab\synthetic-cli-data\runtime.json --access E:\evernook-lab\synthetic-cli-data\mcp-access-owner.json
```

查询 stdin 形如 `{"query":"乌龙茶","mode":"exact","limit":10}`，可选 `since/until/offset` 同[词法契约](lexical-v1.md)。检索是字面匹配、按既有时间顺序分页，不是语义或最新/最相关排序。重复保存必须复用完全相同的来源和内容；不根据文本相同合并真实事件。

成功 stdout 为一行 UTF-8 `{"contract_version":1,"ok":true,"result":...}`，退出码 0；失败为 `{"contract_version":1,"ok":false,"error":{"code":"..."}}`，退出码 1。帮助信息除外。错误不回显正文、命令参数、访问 token 或异常栈；未知本地异常为 `local_operation_failed`，参数/JSON 形状错误为 `invalid_arguments`，非法 UTF-8 为 `invalid_encoding`。核心错误码和未保存状态原样保留。

## 安全与接入边界

- `sensitive=true`、`protected_source=true` 或已识别凭证信号会只保存整条占位投影；`projection_only / original_saved=false` **不能显示成凭证已加密保存**。没有受保护原件可恢复。有限检测仍可能漏检，不能用于保管真实密码。
- CLI 直接持有本地访问能力，`pair/remember/refresh-index/rebuild-index` 不是模型查询接口。不得把 CLI shell 执行权作为 MCP 查询模式的授权替代。目录 ACL 不隔离同一 Windows 账户内的恶意进程。
- 每个入口用自己的 client/entry，通过可信宿主提供真实角色、来源、顺序和时间；模型填写 `role=user` 不构成用户证据。CLI 本身无法验证宿主事件真实性。
- 默认 [MCP `recall` 模式](mcp-v1.md)用于模型查询；可信宿主才显式启用 `trusted-host`。共享普通记忆仍按已确认契约执行，没有新增项目或用途隔离。
- MCP/CLI/Skill 均不保证每轮自动保存和注入；原生采集屏障、流式保全和真实宿主验收仍由 T10 等票据完成。

验证：`python -m unittest discover -s tests/contracts -p test_cli.py -v` 只需核心；完整协议回归使用 `.[dev]` 后执行 `python -m unittest discover -s tests/contracts -v` 与 `python -m mypy`。缺少可选 `mcp` 时，不能把导入失败计为完整套件通过。
