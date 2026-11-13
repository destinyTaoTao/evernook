# 本地 BGE 中文 embedding 冻结小集筛查

两次隔离筛查中，`Xenova/bge-small-zh-v1.5` q8 的 raw 候选通过／失败／缺失均为 **25／2／6**，引文均为 **23／4／6**。此前同一冻结集上的 MiniLM 为 21／6／6、19／8／6；E5 为 24／3／6、22／5／6。BGE 没有重现 E5 在 `insufficient-initial` 的无证据误召回。但四个 arm 和多个阶段仍大面积缺失，两次完整报告均为 `failed`、准入为 `pending`。这些候选仅在不同时点、不同实现快照上筛查，不是严格同时对照，也不构成模型或日用准入。

两次剩余候选失败都是应当没有答案的场景：`control-continued` 召回了 `suppress-confirm`，`revoked-continued` 召回了 `revoke-confirm`；结果中没有被禁止的原来源。引文另有 `interest-continued`、`correction-continued` 失败。27 个已运行场景的语义索引状态均为 ready，实际结果中有语义通道，不能把这些结果简单归因于词法回退。六个未采集案例不能计作通过。

| BGE 实验 | 27 次召回 p50／p95 | 树 private bytes 采样峰值 | 进程查询失败 |
|---|---:|---:|---:|
| 第 1 次 | 471／502 ms | 170.3 MiB | 7 |
| 第 2 次 | 473／512 ms | 167.4 MiB | 6 |

时间只含 recall，不含建库或宿主回答；p95 是少量混合场景的描述值。250 ms 树采样有漏采和查询失败，不能视为硬峰值或与此前 E5／MiniLM 的峰值作可靠比较；未测 CPU、长驻、多客户端、大库、生成中取消和正式 p95。未更改阈值、金标准或评分器，也没有使用真实用户数据。

本实验使用 [原模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5) 的中文查询指令和 CLS pooling，文档不加指令；使用 [Xenova ONNX 转换版](https://huggingface.co/Xenova/bge-small-zh-v1.5) 固定 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`，q8 ONNX SHA-256 `15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc` 与仓库元数据一致。逐次验证资产哈希并使用 512 维独立空间。模型权重不在本仓库，转换资产的分发许可链尚未验收。

复现使用 [bge_candidate.py](../../probes/admission/bge_candidate.py)、[bge_worker.mjs](../../probes/admission/bge_worker.mjs) 和现有 `monitor.py`，冻结 suite SHA-256 为 `b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`。探针通过进程内测试替身注入采集器，使用当前真实查询／文档分离接口，不修改生产 provider 选择；worker 限制网络调用但不构成操作系统级网络隔离证明。路径、结果和进程采样保存在仓库外 `C:\Users\dengtaotao\Documents\Codex\2026-11-14\evernook-bge-candidate` 与 `evernook-bge-monitor-v1/v2`。首次运行结束后仅删除了候选脚本中的未使用导入；第二次结束后仅整理了 worker 末尾空行。

```powershell
$env:PYTHONPATH='src;probes/admission;.'
python probes/admission/monitor.py --output <new-monitor-dir> --max-seconds 420 -- python probes/admission/bge_candidate.py --node <absolute-node> --model-dir <bge-assets> --transformers-dir <transformers-package-dir> --output <new-result-dir>
```

模型没有接入服务或开启日用。下一步应独立定位确认材料导致的误召回，补全冻结矩阵、许可证、资源和宿主验收，再判断是否允许配置；不能通过修改冻结评分或屏蔽安全失败来准入。
