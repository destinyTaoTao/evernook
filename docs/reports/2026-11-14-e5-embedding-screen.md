# 本地 E5 embedding 冻结小集筛查

## 结论

`Xenova/multilingual-e5-small` q8 在本机合成小集里表现出值得继续研究的候选召回改善，但出现新的无证据场景误召回，资源样本波动较大。**未接入服务，也未通过日用准入。** 后续需要独立质量、规模、并发、取消、资源和完整宿主验证，不能把这里的样本当作正式 p95 或产品保证。

原模型卡要求检索查询和文档分别使用 `query: `、`passage: ` 前缀；模型的相似度数值分布也与 MiniLM 不同。[原模型卡](https://huggingface.co/intfloat/multilingual-e5-small)；[Transformers.js 转换版](https://huggingface.co/Xenova/multilingual-e5-small)。当前生产 `EmbeddingBackend.embed` 没有区分两种用途，因此不能把现有调用直接换成该模型或沿用旧向量。转换权重的分发许可链尚未验收，本项目不分发模型。

## 方法与结果

同一源码 `fa15411`、同一机器、就绪词法索引和冻结 `zh-learning-v1`（SHA-256 `b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`）分别运行原 MiniLM 和 E5；未改金标准、评分器或 0.4 阈值。两者均只有 raw arm 的实际 Core 候选／证据及规则画像观察，其他 arm、宿主答案、恢复与处理器换版仍缺失。小集数据是合成数据，不是用户记忆。

| 配置 | 候选通过／失败／缺失 | 引文通过／失败／缺失 | 27 次召回 p50／p95 | 树 private bytes 采样峰值 |
|---|---:|---:|---:|---:|
| 原 MiniLM 控制 | 21／6／6 | 19／8／6 | 1022／1085 ms | 548 MiB，2 次读取失败 |
| E5，逐次校验资产 v2 | 24／3／6 | 22／5／6 | 1039／1073 ms | 872 MiB，2 次读取失败 |
| E5，可复现探针 v3 | 24／3／6 | 22／5／6 | 1055／1141 ms | 555 MiB，2 次读取失败 |

延迟只覆盖一次 recall 调用，不含建库、画像处理或宿主生成；p95 为小样本 nearest-rank 描述值。峰值按 250 ms 采样，不是硬峰值；v2 与 v3 的差异原因未查明。E5 相比控制改善了部分旧记忆场景，但 `insufficient-initial` 从通过变为失败，`control-continued` 与 `revoked-continued` 的候选失败仍存在。E5 v3 的评分结果仍为 `failed`，admission 为 `pending`。

首次 E5 v1 仅在启动时校验资产，候选／引文同为 24／3／6、22／5／6，p50／p95 为 930／975 ms；与逐次校验的控制不等价，不据此宣称性能收益。实际模型的 tokenizer／图文件均在仓库外，下载自固定 revision `761b726dd34fb83930e26aab4e9ac3899aa1fa78`；ONNX SHA-256 `f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193` 与该 revision 的 Hugging Face 元数据一致。Node worker 报告零网络尝试，不能替代操作系统级网络隔离证明。

## 复现和边界

使用 [e5_candidate.py](../../probes/admission/e5_candidate.py) 配合现有 `monitor.py`、可信本地 Node 24.11+、Transformers.js 4.2.0 和 ONNX Runtime Node 1.24.3；各路径及 `--output` 均指向仓库外新目录。候选脚本仅接受指定版本资产和冻结 suite，区分 query／passage，按次校验文件；它通过进程内测试替身注入现有采集器，不修改生产接口。脚本仅检查调用前取消，未验证生成中取消或超时后的全部资源释放。

```powershell
$env:PYTHONPATH='src;probes/admission;.'
python probes/admission/monitor.py --output <new-monitor-dir> --max-seconds 420 -- python probes/admission/e5_candidate.py --node <absolute-node> --model-dir <e5-assets> --transformers-dir <transformers-package-dir> --output <new-result-dir>
```

本次 v3 在脚本完成运行后只调整了类型注解与等价的显式模块导入；最终脚本单独完成一次真实本地 query smoke，尚未以最终文件再次跑完整小集。原始三个 E5 运行、MiniLM 对照、监控 samples、失败与 collection 保存在仓库外 `C:\Users\dengtaotao\Documents\Codex\2026-11-14\evernook-e5-candidate`、`evernook-e5-monitor-v1/v2/v3`、`evernook-minilm-control` 和 `evernook-minilm-monitor-v1`。探针测试不会启用模型、不向云端发送文本；临时输出目录需保持隔离。
