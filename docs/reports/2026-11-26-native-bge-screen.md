# 中文 BGE 的 Python ONNX Runtime 与 Node 对照

固定 `a95f611` 的 Core、同一 BGE q8 模型及冻结中文 33 题，Python 常驻 CPU session 和 Node 单次 worker 得到相同的逐题评分。raw arm 的候选召回 29/33、引文 26/33、有限类别自动更新 33/33；四个控制状态题仍有误召回。四个实验臂合计 792 个评分单元，本次采到 99 个，88 通过、11 失败，693 个未采。宿主回答、反思质量与日用不在这组测量范围。

模型固定为 Xenova/bge-small-zh-v1.5 的 `75c43b069aac4d136ba6bc1122f995fedcfd2781`，逐项核对六个模型资产摘要。中文查询使用检索指令，文档使用原文，输出 512 维向量。Python 3.14.4，NumPy 2.5.5、ONNX Runtime CPU 1.32.0、tokenizers 0.22.2；ORT 设置两个算子内线程和一个算子间线程。两个实现使用独立向量空间。8 条文档与 4 条查询的单独配对中，四次文档排序相同，最低配对 cosine 分别为 0.99999976 和 0.99859560。查询最大坐标差 0.006337，切换实现时应重建派生向量。[固定模型及前期小集结果](2026-11-14-bge-embedding-screen.md)

| 独立进程树，完整 33 题 | 公开召回 p50 / p95（毫秒） | 整轮耗时（秒） | 最大观察到的树 Private Bytes（MiB） | 采样次数 / 进程查询失败 |
| --- | ---: | ---: | ---: | ---: |
| Node，每个 batch 启动 worker | 457 / 2919 | 481 | 224.6 | 1916 / 0 |
| Python ORT，默认外围线程 | 79 / 2302 | 248 | 782.2 | 989 / 1 |
| Python ORT，限制外围线程 | 87 / 2420 | 232 | 171.0 | 928 / 0 |

第三行仅在启动实验进程时设置 `OPENBLAS_NUM_THREADS=1`、`OMP_NUM_THREADS=1`、`TOKENIZERS_PARALLELISM=false`。三项一起改变，无法从本轮分配各项贡献。每臂独立创建合成库、实际恢复归档、执行确认删除并调用已安装的历史 0.1.12 检验处理器变更。33 题包含不同大小的库，延迟覆盖公开召回，整个进程树以 Windows Job 每约 250 毫秒采样，Private Bytes 表示已提交的私有虚拟内存。各臂各一个资源窗口，同机有并行任务。现有 Node 常驻服务和 120 秒轮换的连续负载未在这组对照中运行。

实验后端只可由探针选择，使用本地资产，生产包没有新增依赖。探针在原生调用前后检查时间和取消。将其接入长期服务仍需验证运行中抢占、故障恢复、固定资产分发许可和真实宿主质量。

在独立评测环境安装上述三个版本，并设置 `PYTHONPATH` 包含导出的 `src` 和仓库根目录。配对向量使用下列第一条命令。分别取 `$backend` 为 `native` 或 `node`，用第二条命令采集各自的冻结结果及进程树。受限线程组在启动该命令前设置上述环境变量。每组使用全新的仓库外目录。[探针](../../probes/admission/bge_onnx_screen.py) · [冻结评测](../../eval/zh-learning-v1)

```powershell
python -B -m probes.admission.bge_onnx_screen --model-dir $model --transformers-dir $transformers --node $node
python -B -m probes.admission.monitor --output $monitorDir --max-seconds 1200 -- $python -B -m probes.admission.bge_onnx_screen --model-dir $model --transformers-dir $transformers --node $node --frozen-backend $backend --frozen-output $resultDir --previous-core-python $previous
```

本轮本机原始报告保存在临时目录 `ev-bge-pair-e7964d7ed1df41fca01669a40db8c05a`。表中三行对应 `isolated-node`、`isolated-native` 和 `limited-native`。这些运行各自只启动选定后端。
