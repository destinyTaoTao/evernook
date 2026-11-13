# #86 — CPU输出所有权与2240token固定请求对照

2026-11-08，接续[六题变化输入反例](m58-quality-pilot-2026-11-08.md)。本批固化真实ORT小回归，确认固定版本的CPU预分配输出仍被复制；关闭内存模式或每步强制GC均未取得足够资源收益。原生补丁只准备了可审查提案，尚未应用、编译或接入，另行等待维护者确认，不以已有dsh宿主JS补丁授权代替原生二进制取舍。原版本、模型、默认profile、评分和日用门禁保持。

## 最小实际边界

`plugins/harnesses/probes/output-ownership.ts PROFILE NEW_EXTERNAL copy|reuse`不运行Qwen。116字节的合成ONNX图为float32[4]的`Add(x,[1,1,1,1])`，opset17/IR8，SHA-256 `0d496258c98ed83808e440e2381474e648f4de664a78bfd69e366e9bb088a19d`。由ONNX helper生成，不含预训练或私有数据，执行不依赖ONNX Python包。

实际Windows x64 ORT1.24.3中，调用方传入带非零byteOffset的预分配Tensor：其四个数值被正确写入，前后哨兵不变，但返回Tensor和ArrayBuffer都不是调用方对象；修改调用方首值，返回值仍保留旧值。错误shape拒绝，session释放后返回数据仍可读。`copy`模式0退出，`reuse`模式在相同实际调用上以`cpu_output_ownership_mismatch`非零退出；完整回执先写出，不能把“预分配被填充”解释为“返回零复制”。两次各15秒硬操作上限，约0.29秒完成；短进程仅一条采样，不用于宣称内存峰值。

该小回归记录实际二进制身份：

- `onnxruntime_binding.node`：`32a61411861578b3e0988e669e9d9a474cc1436369269df0a4e23765cbe9e75a`。
- `onnxruntime.dll`：`4bc168111012971ae1ffef36c1f29c06b026d17e9063bd17f6390359fbe32a84`。

固定[v1.24.3运行边界](https://github.com/microsoft/onnxruntime/blob/v1.24.3/js/node/src/inference_session_wrap.cc)将预分配值交给原生Run，返回仍走[CPU ArrayBuffer复制](https://github.com/microsoft/onnxruntime/blob/v1.24.3/js/node/src/tensor_helper.cc)。这说明当前API调用方式不能省去返回复制，不据此宣称违反全部API契约。

本次registry核对latest为1.31.1，其[版本源码](https://github.com/microsoft/onnxruntime/blob/v1.31.1/js/node/src/inference_session_wrap.cc)仍保留上述返回转换；未安装或实测其二进制。上游[固定main提交1f84d530](https://github.com/microsoft/onnxruntime/blob/1f84d5308115bcb96085f1202e21c9a108d37fbe/js/node/src/inference_session_wrap.cc)已有数值预分配直接返回调用方对象的分支。不能把main能力冒充已发布版本能力，未升级依赖或下载开发构建。

## 固定长输入，不减输入输出

从六题第一批g01-q1保存的实际provider请求捕获一份完整只读快照：2240输入token，完整生成自然结束128token，保留32768窗口、4096记忆及512输出额度。同一外置/预处理权重候选、十项完整资产校验、CPU4/1、noarena和既有协议不变。比较真实prepared feeds/全部57个输出以及完整token IDs/文本；没有截短KV、跳过logits或用答案修改payload。

| 受控窗口 | Private Bytes峰 | 工作集峰 | 秒数/上限 | 样本/query failure |
|---|---:|---:|---:|---:|
| 单prefill基线 | 1574125568 | 1656561664 | 19.305/35 | 77/0 |
| 单prefill关闭MemPattern | 1558859776 | 1971085312 | 19.307/35 | 77/0 |
| 串行单次完整生成基线 | 2580762624 | 3174772736 | 46.091/75 | 184/1 |
| 串行单次完整生成每步强制GC | 2269220864 | 2869985280 | 47.839/75 | 191/0 |

prefill两组的所有feeds/56KV及最后logits逐字节一致；关闭内存模式的单样本Private差异很小，不作为修复。完整两组都是128次forward，token IDs/文本与原生捕获参考完全一致。每次输出最高543540736字节；调用后的JS ArrayBuffer最高1087620863字节，GC组同样如此。强制GC降低观测Private Bytes，但仍高于2147483648，不证明已定位单一分配器或持续泄漏，不要求日用Node暴露/强制GC。

GC后的调用边界不能观测C++转换内部的短暂分配，也不能把外部内存计数直接当作实际物理驻留。所有窗口250ms采样、OS缓存未控制，第一组完整生成query failure1如实保留；没有硬峰值或p95承诺。

## 保留的一次窗口重叠

最初两轮完整基线在第二轮仍运行时误启动GC诊断，两个窗口部分重叠。发现后通过新GC窗口STOP停止其owned tree，回执为operator_stop、root_exited=true、stop_error=null；相关进程最终清理。原两轮基线96.406秒/385采样、峰2281205760字节，两个完整128token输出仍与参考一致；受重叠影响，不用于干净耗时对照或优选较低峰。被停止GC窗口30.938秒/121采样、45次forward，不称为完整生成。

随后以一个父命令同步执行两组新的单次完整生成，上一组正常终止才启动下一组。上表使用这两组，旧回执/部分输出和源码保留，没有覆盖或把两轮与单轮混成相同吞吐指标。

## 原生提案与剩余门禁

仓库外已准备7行分支提案：仅对原生成功返回的数值预分配Tensor返回调用方对象，字符串维持原转换；保持原ORT1.24.3 DLL，计划只重建独立Node binding。版本源码与上游固定提交分别保留哈希，工具链已只读检查。本机已安装VS BuildTools，但尚未因此编译、替换或加载任何补丁二进制。

此路线会增加原生补丁构建/身份/ABI/复验维护成本，已通过用户输入入口请求是否允许仓库外独立实验，未把等待或时间经过当作授权。若允许，仍须先小图缓冲区偏移/形状/释放后读取，再验证Qwen完整feeds/KV/logits/多轮token、取消与原生回放及变化输入资源；补丁存在不等于已达标，不能自动晋升。

TypeScript typecheck/build、实际小图copy通过/reuse反例、长输入feeds/输出和四次完整128token等价性、差异检查通过。没有修改Core/宿主、安装全局依赖、运行224题、改变资源阈值或放开日用。原模型无依据/授权/冲突决策失败继续由#81保留，#86/#81仍开放。

外部回执、固定请求、介入源码、提案与源码哈希保存在`Documents/Codex/2026-11-08/evernook-86-output-ownership-01`。默认资产和未通过记录保留，不提交模型、运行库或生成输出。
