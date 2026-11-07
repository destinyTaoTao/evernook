# #86 — 外置原始权重与预处理权重的独立候选

后续[六题变化输入](m58-quality-pilot-2026-11-08.md)两批Private Bytes峰2484412416/2238353408字节，均再次超过2GiB，质量也未通过。下文固定请求成绩保留，不代表全窗口准入。

2026-11-08，接续[多余正文读取修复](m58-followup-86-path-cache.md)。本批交付可重复导出器与显式实验profile；固定请求的Private Bytes采样已低于2GiB，但工作集、质量、变化输入和完整准入仍未闭合。没有替换原图/旧profile、第三方包、日常provider或冻结policy。

## 复现与取舍

同一last-hidden图、1321输入token、32768窗口、4096记忆预算、512输出上限、CPU4/1线程。基线使用已修复文件读取的profile并关闭arena；单次prefill在13.550秒内完成，但峰2389196800字节，2GiB断言失败。后续回归同一旧profile仍为2375159808字节、输出完全相同，没有把旧profile静默换成新候选。

依次验证三条解释：序列化模型复制、权重预处理常驻，以及推理KV/JS输出叠加。固定ORT1.24.3支持[ORT字节直接加载与初始化器生命周期选项](https://github.com/microsoft/onnxruntime/blob/v1.24.3/include/onnxruntime/core/session/onnxruntime_session_options_config_keys.h)，但实测直接加载ORT候选反而更高，拒绝采用。ORT导出文件1541650032字节，导出峰4662661120；直接字节加载明确保持Buffer至session释放，prefill峰3231178752。所有57个输出仍一致，未把正确性当成资源通过。两次预加载启动错误（Windows import URL、原型括号）及对应失败回执保留，修复后另建monitor，未覆盖失败。

同版本提供外置initializer及保存预处理权重选项。其[文件读取实现](https://github.com/microsoft/onnxruntime/blob/v1.24.3/onnxruntime/core/framework/tensorprotoutils.cc)先尝试映射，失败可退回复制；[session实现](https://github.com/microsoft/onnxruntime/blob/v1.24.3/onnxruntime/core/framework/session_state.cc)可使用磁盘预处理权重。实际结果支持这条路线，但不承诺其他平台映射成功，也不把Private Bytes减少等同于物理内存等量减少。

## 实现与资产身份

新增`plugins/harnesses/probes/export-external-qwen.ts`，只接受固定last-hidden父profile，先检查八个配置/tokenizer文件及父图哈希，再以原ORT版本/线程设置导出ONNX外置权重和预处理权重。原始模型不写入，输出必须位于checkout外、与源不重叠且不存在；部分失败不生成可用profile，不覆盖重跑。新生成文件必须匹配独立固定哈希，不能靠自报manifest接受任意资产。导出操作仍需45秒限时，导出本身峰3111936000字节，**未通过2GiB**；它是离线准备，不是日用加载成绩。

新profile为`qwen3-0.6b-last-hidden-external-prepacked-experimental-v1`，实验loader仅对它显式关闭arena、要求verified-local-path-v1，加载前和每轮生成前完整核对十个文件。其余profile保持原分配与资产身份。因为ORT提示优化产物可能绑定硬件，首版仅接受Windows x64、`13th Gen Intel(R) Core(TM) i5-13600K`、Transformers.js4.2.0/ORT1.24.3；其他环境拒绝，不宣称可移植。CPU名称检查不构成对虚拟化/全部CPU能力的完整证明，确切资产不匹配时也拒绝，没有自动退回或重新量化。

| 文件 | 大小/固定SHA-256 |
|---|---|
| 父图 | 919096911字节；`e9370b411c56b37bfeb48f2787655c62b014eb17d8fb441d3fc281ef0d01e3ce` |
| 新`onnx/model_q4.onnx` | 343596字节；`2f72c2c1990f6039bece24a8441e85d9ed1836f6256afd96b513803b531a4ef7` |
| 新`onnx/weights.bin` | 2163445760字节；`d857c47c0d21141b9082a0d5806603234bfaad1a0383be2905afb55d7882dcba` |

两次独立原型导出及正式导出器输出上述图/权重完全一致。ONNX只读核对：父图全部452个大于等于1024字节的raw initializer在候选中名称/形状/类型/原始字节哈希一致，无删除；唯一新增的大initializer是622329856字节的embedding转置，完整uint32位模式分块转置比较相同。没有精度、词表、学习权重或输入额度减少。运行时优化重写了图，未声称撤销后可恢复原protobuf或全图标准checker通过；实际ORT加载和以下等价性是本批验证边界。

## 有界测量

均为250ms进程树采样，OS缓存未控制；GC/profiling/哈希等诊断开销存在，不是p95，也不保证采样间硬峰值。正式集成路径包含外置权重的每轮全量校验；原型preload只用于定位，不替代集成结果。

| 实验 | Private Bytes峰（字节） | 工作集峰（字节） | 总秒数 |
|---|---:|---:|---:|
| 原路径prefill，noarena | 2389196800 | 2302672896 | 13.550 |
| 仅外置权重prefill，noarena | 1656926208 | 1730957312 | 13.803 |
| 外置+预处理权重原型prefill，noarena | 1088860160 | 1434476544 | 14.301 |
| 正式集成候选prefill | 1028915200 | 1552990208 | 14.552 |
| 正式集成候选两轮完整生成 | 1802911744 | 2405994496 | 56.351 |
| 取消后同实例完整生成 | 1780641792 | 2373570560 | 36.083 |

两轮集成生成的Private Bytes与上一批相同请求/noarena的3095154688字节相比下降；上一批工作集未作为本表比较基线。新候选工作集仍超过2GiB，不把文件映射描述为“无需物理内存”。外置权重还增加磁盘与校验成本。prefill各35秒、两轮100秒、取消复用60秒硬上限；所有有效模型测量正常退出、query failure0。集成prefill58样本、两轮225样本；原型与正式各两轮均为自然119token，所有IDs/文本匹配原参考，全部输入feeds/56KV/最后logits逐字节一致。

## 安全、原生流与取消

复制模型中的外置权重改一字节，加载在创建原生session前拒绝；恢复该字节后可以加载。新图用旧profile身份拒绝。加载后仅向复制配置追加换行，下轮在forward前拒绝；该回归create1/forward0、外网尝试0。六项配置/临时缓存并发回归通过，包含新profile不能退回默认文件访问；原八个配置/tokenizer哈希与原始模型资产均保留。真实导出CLI另验证checkout内输出、源重叠、错误父profile及已有目录四项拒绝；现有候选profile/manifest/图哈希不变，错误目标未创建。

实际dsh SDK0.2.1-rc.3/Core新合成夹具各只存原题允许的一条来源，真实MiniLM索引1条，不向模型提供黄金答案。独立来源UUID和上下文使输入不同于1321token固定请求，不把两者当作逐字节相同的模型对照。它沿用固定诊断环境，不是rc.5 Web完整工具验收：

- 正常流23.811秒、95采样、峰1678180352字节；972输入/71输出token，可见137字符，首段独立读取与最终回放一致。首个可靠可见片段6549.679ms，未改善为正式延迟准入。冻结host-g01-q6评分仍failed，回答没有满足逐字引用。
- 取消14.051秒、56采样、峰1255239680字节；982输入/2输出token，aborted/canceled、complete=false、final_message_id=null。质量观察明确diagnostic_canceled，不计成功答案。
- 清理宿主后用新MCP客户端重复读取并重开，正常全文与取消前缀逐字一致，6.543秒/26采样，峰226091008字节。另在同一模型实例取消固定1321token请求后完整生成119token，与原参考一致。

上述query failure/外网尝试均0，取消不是副作用回滚声明。未重跑Core全量、SDK全量、pi、224题或变化输入长跑；六项配置/并发测试、TypeScript typecheck/build和差异检查通过。

外部原始回执、拒绝路线、导出/权重核对与脚本位于`Documents/Codex/2026-11-08/evernook-86-ort-format-01`。逻辑文件总量约11.77GB，含多个独立候选/失败诊断，不等于单个安装体积；没有提交权重、会话、配置或凭据。#86/#81继续开放，模型与正式日用未晋升；变化输入/全质量、资源全窗口、规模、真实目标模型及#82物理OS门禁保持。
