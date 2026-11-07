# #86 — last-hidden候选的CPU arena对照

2026-11-08，接续[#86](https://github.com/destinyTaoTao/evernook/issues/86)及[既有候选验证](m58-followup-86-last-head-candidate.md)。这是有界诊断，候选未晋升；默认provider、模型图、精度、冻结policy和评分均未替换。

## 固定输入与单变量

同一Qwen3-0.6B last-hidden候选图`e9370b411c56b37bfeb48f2787655c62b014eb17d8fb441d3fc281ef0d01e3ce`，大小919096911字节；八个其他资产保持原哈希。请求SHA-256为`2dca8d8deafcca38d3b49f1440506a3899d0b63a4189e6e2b2cc0bb2b2fc2cc2`的既有合成捕获请求，实际1321输入token，32768窗口、4096记忆预算及512输出上限不变。

机器重新核对为i5-13600K、20逻辑处理器、68448100352字节内存，Node24.12.0/Python3.14.4；Transformers.js4.2.0、ORT1.24.3、CPU4/1线程沿用固定资产。关闭arena只传`enableCpuMemArena: false`，实际session-options逐项比较只差该字段；prefill另去除每次输出目录不同的profiling路径再比较。没有同时改变prepack或其他优化。

`prefill-resources.ts`增加`noarena`，仍捕获真实prepared feeds再执行一次原生ORT。`model-resources.ts`保留原`reuse/rebuild`语义，增加可选第五参数`default|noarena`及真实session-options、阶段绝对时间戳；参数默认保持原行为。两轮生成复用同一模型实例。

模型创建参数元数据和完整生成阶段的绝对时间戳在四个主实验之后补入。旧回执按各自manifest的实际源码版本保存，未补造这些字段；新增创建参数元数据已通过独立loader-input-proof实跑核对。

## 结果

| 同请求诊断 | 默认arena | 关闭arena |
|---|---:|---:|
| 单次prefill采样进程树PrivateBytes峰值 | 3246940160 | 2701029376 |
| prefill整体秒数 / 硬操作界限 | 14.048 / 35 | 13.548 / 35 |
| 两轮完整生成采样峰值 | 3959406592 | 3216777216 |
| 两轮整体秒数 / 硬操作界限 | 42.838 / 100 | 53.848 / 100 |
| 第一轮生成毫秒 | 19294.720 | 24624.928 |
| 第二轮生成毫秒 | 18384.756 | 24069.634 |

四个主实验各自0退出，250ms采样56/54/171/215次，内存查询失败及外网尝试均0。采样可能遗漏瞬时峰值，不保证硬内存上限。实验依次运行，OS缓存未控制；部分prefill与类型检查共享机器。这些是小样本原始耗时，不是p95或速度保证。

两种分配配置的所有输入feeds、56个KV输出及最后logits行逐字节一致。四次完整生成均自然结束于119token，全部token IDs/可见文本相互一致，也与11月5日保留的原候选结果一致；没有减少输出额度或提前截断。既有严格canonical失败不因此消失，没有重跑224题或正式质量对照。

关闭arena确实降低观察峰值，但完整两轮仍为约3.22GB，高于2147483648字节门槛，且本批生成更慢。不能晋升为默认选项，#86/#81保持开放。

## 时间区间与被否定的哈希假设

默认prefill的最大采样在`feeds_ready → outputs_held`；关闭arena后的最大采样移至`model_loaded → feeds_ready`。后一组结果持有阶段最近采样约2.10GB、释放后约1.80GB，不能用这些较低阶段值替代全窗口峰值。

临时边界探针对两次资产复验加绝对时间标记，未改模型运算。有效attribution-03记录第二次复验区间覆盖2713837568字节峰值，但**进入复验时**ArrayBuffer就已约919MB。把哈希读取替换成固定64KiB缓冲区后，峰值反而为2778431488字节；全部prefill输出仍相同。该试验否定了“持续分配哈希读取块就是剩余峰值原因”的修复假设，修改与仅为该修改新增的测试均已撤回，未改变默认校验路径。边界重合不能证明分配由哈希产生，也不能把V8内存计数变化解释为原生资源释放。

首次临时探针因相对源文件路径错误，在模型加载前失败；第二次插桩位置晚于复验，没有取得边界记录。这两次及未改善的固定缓冲区试验均保留，不纳入成功归因。外部副本保留各版本源码与manifest哈希，后续源文件变化不覆盖原记录。

新增的ORT创建边界元数据证明当前库传入的是**路径字符串**，并非应用直接提供模型字节数组；该proof仍核对候选身份、全部prefill输出相同及外网0。919MB ArrayBuffer接近完整图大小是进一步调查线索，尚未证明它的所有者或复制栈。

固定ORT1.24.3的[官方配置头](https://github.com/microsoft/onnxruntime/blob/v1.24.3/include/onnxruntime/core/session/onnxruntime_session_options_config_keys.h)将直接借用模型字节的开关限定于ORT格式，并要求字节在整个session期间有效；不能给当前ONNX路径随手加开关便声称零复制。后续可独立验证载入字节所有权及ORT格式候选，但本批没有导出或晋升该候选，也没有采用仅在上游main出现的新选项。

## 可复验入口与边界

```text
node --expose-gc plugins/harnesses/probes/prefill-resources.ts PROFILE REQUEST NEW_EXTERNAL noarena
node --expose-gc plugins/harnesses/probes/model-resources.ts PROFILE REQUEST NEW_EXTERNAL reuse default
node --expose-gc plugins/harnesses/probes/model-resources.ts PROFILE REQUEST NEW_EXTERNAL reuse noarena
```

各命令须由`probes/admission/monitor.py --max-seconds 35|100 -- ...`包装，使用新外部目录；不覆盖或复用旧输出。类型检查、构建及差异检查通过。原生/durable取消代码未改变，本批不宣称重新验收取消、pi、桌面或真实远端模型，也不将本批直接ORT诊断冒充dsh0.2.1-rc.5完整链路。

外部证据位于`Documents/Codex/2026-11-08/evernook-86-arena-01`，包含四个主实验、独立比较回执、attribution失败/有效记录、撤回变体源码及loader-input-proof。模型、请求、数据库、原始输出与任何凭证均未提交。
