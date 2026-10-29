# T03：凭证候选的合成可行性验证

对应 [Issue #4](https://github.com/destinyTaoTao/evernook/issues/4)。使用 PyNaCl/libsodium 的现成密码学原语验证锁定收件与本地授权边界，没有实现产品保险库或开放 HTTP/MCP 接口。仅允许合成凭证；结论见[报告](../../docs/reports/r0-credentials-2026-10-29.md)。

## 复现

已测 Windows 11 x64、Python `3.14.4`。解释器需有标准库 Tk；GUI 测试构造真实隐藏控件，不弹出无人操作的密码框。依赖锁文件固定 PyNaCl `1.6.2`、mypy `1.18.2` 及传递依赖和分发哈希。示例虚拟环境、运行数据均在仓库外。

```powershell
Set-Location E:\project\evernook\probes\credentials
uv venv E:\evernook-lab\credentials-venv --python 3.14
uv pip sync requirements.txt --python E:\evernook-lab\credentials-venv\Scripts\python.exe
E:\evernook-lab\credentials-venv\Scripts\python.exe -m mypy --strict vault_probe.py local_ui.py test_vault.py crash_worker.py run_probe.py
E:\evernook-lab\credentials-venv\Scripts\python.exe run_probe.py --output E:\evernook-lab\new-credentials-run
```

输出目录须不存在，父目录须已存在且不在 checkout 内；不覆盖旧证据。首次依赖准备需要联网，探针没有模型或网络服务。单独运行 `python -m unittest -v test_vault` 用系统临时目录，证据 runner 则保留各测试的密文、公开材料和数据库。

## 证明了什么

- SealedBox 用可信初始化的接收公钥加密；发送端不持接收私钥，无法解密自己的密文。SealedBox 不证明发送者身份，另用 Ed25519 签名绑定密文和对象/版本/来源/密钥 ID；固定的受信任 VerifyKey 来自实验初始化，不能由请求自行替换。
- Argon2id（libsodium interactive 参数）派生密钥，SecretBox 加密接收私钥。盐与算法标记可落盘，口令只在生成/本地控件/helper 内存中；脚本参数、环境变量、日志和文件不接收口令。
- 加密原件与关联状态分两次事务提交；第一步返回 `protected_pending_link`，第二步才返回 `protected_linked`。当前关联是同一收件库的 linked 标记，验证中断与幂等补齐原语，未接真实普通账本或脱敏投影。真实子进程在两个窗口直接退出，重启后补齐状态；不是断电测试。
- 单个 helper 的授权绑定调用端签名、对象版本、操作和用途，短时一次性；8 路并发只交付一次，过期/撤销/重启/显式锁定/已换版本拒绝。普通收件路径没有解密密钥。
- 合成恢复演练包含加密私钥恢复、对象备份、新旧密钥版本及错误密钥失败；无恢复材料不提供绕过。演示原文和密钥未出现在扫描的 SQLite/WAL/备份/日志文件中。

## 未证明的边界

这是分离的原语、隐藏 GUI 控件和内存授权实验，不是已认证的完整取密产品。`approved=True` 只由受信任本地确认后调用的假定不能直接作为公共 API；固定公钥不代替生产配对/文件 ACL/IPC 身份认证。未测人类确认界面、OS 锁屏事件、跨进程并发授权、撤销后的旧备份恢复、恶意输入限额或日常 harness 日志回流。Python 对象释放不保证所有口令/私钥副本清零，进程分离也不抵御同用户恶意软件。

`local_ui.py` 的默认展示只更新本地 Label，自动测试在隐藏 Tk 窗口输入合成随机口令，读取结果后清空；没有向模型或真实 harness 交付。生产启用仍被上述缺口阻止。

PyNaCl 声明 Apache-2.0，依赖/打包的第三方许可须随发布保留。本机没有 KeePassXC；其 `2.7.12` 文档仅作另一个成熟候选的接口/许可评估，未安装或操作已有保险库。没有提交虚拟环境、密文样本、运行数据、口令或密钥。
