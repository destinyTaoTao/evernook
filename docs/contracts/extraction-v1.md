# T12 显式离线抽取执行增量

`evernook.extraction.ExtractionProcessor` 领取既有候选租约，在数据库事务之外运行有界本地模型，再调用同一 `CandidateWorkflow.complete` 核对来源版本并提交。任务成功/完成水位与候选仍为同一事务；不执行事实接纳，不修改人设或调度后台任务。

当前唯一内置profile为 `qwen3-0.6b-experimental-extractive-v1`：复用R0已准备的 `onnx-community/Qwen3-0.6B-ONNX` 固定revision `58ca03a5e7b354b480b1a82b34bde8dc7b854cc0`，q4 CPU，Node24.11.x、Transformers4.2.0、onnxruntime-node1.24.3。每次核验九个本地文件的固定SHA-256，processor版本绑定模型/协议/依赖及实际worker脚本（含prompt）的SHA-256；缺失、改动或依赖不符失败，不下载、不退云。此profile仍未通过事实抽取质量准入，不能据此启用日常自动沉淀。

配置文件必须在仓库外，由可信本地操作端提供：

```json
{"schema_version":1,"profile":"qwen3-0.6b-experimental-extractive-v1","node":"E:/configured-node/node.exe","transformers_dir":"E:/configured-node-packages/@huggingface/transformers","model_dir":"E:/configured-local-model"}
```

从UTF-8 stdin调用：

```text
<python> -m evernook.cli extract --config <runtime.json> --access <access.json> --extraction-config <profile.json>
{"source_ids":["<saved-message-id>"],"timeout":120}
```

`source_ids`最多16条、总计16384个code point；仅处理完整、可普通读取且确实保留的原文，不解密或尝试重建受保护投影。prompt超过4096模型tokens会失败，不能静默截取为完整来源。生成上限512tokens。当前进程每次加载模型，不声称常驻吞吐、GPU加速或p95达标。

## 输出与拒绝

模型只提出原文选择：每项最多16个 `{source:<数组下标>,quote:<唯一逐字片段>}`，外带provenance/disputed。宿主计算code point坐标并将选中片段拼为候选文本，不采用自由生成的事实摘要。片段缺失、重复出现而不能唯一定位、类型/角色不符、敏感信号或超限会明确失败，不猜第一个位置，不补写模型编造的属性。

只接受完整JSON对象，或整个输出恰好是单个JSON代码围栏；拒绝前后指令、残缺JSON、重复键和未知形状。围栏兼容不改变来源校验。用户陈述、助手生成、引用和角色扮演是候选的标记，分类质量仍需评测；普通用户角色并不证明其中没有引用。提示注入不会获得执行权限，但这里没有声称模型能正确理解所有否定、时间或引用边界。

`ExtractionReport.run.state` 与 `error_code` 决定本次任务结果，CLI的 `ok=true` 仅表示成功取得任务回执，**不代表抽取成功**。失败类别持久化为 `canceled/timed_out/invalid_output/input_not_admitted/dependency_unavailable/extractor_failed`；来源变化另显示stale。报告携带更细的有限错误码，不保存原始模型异常/诊断。重试复用来源/processor任务；已成功任务不再调用模型。现有原文精确回取不依赖模型成功。

请求预算最多120秒，在领取后、哈希/推理期间及提交前协作检查；取消和超时会终止并回收模型子进程。已开始的数据库提交不异步打断，身份/ACL和存储调用还受各自超时约束，因此未声称整个API的严格墙钟上限等于120秒。Node禁止fetch/socket并强制local_files_only；这是进程级护栏，未代替系统断网或native库的完整出网验收。

## 验证与剩余工作

真实本地CLI合成实验已得到一项逐字候选，仍保持candidate状态；单个样例不等于模型质量准入。R0曾测出该小模型错误主体；本路径用严格原文选择减少自由生成风险，不能据此抹去质量问题。

复现见 [probe](../../probes/extraction/README.md)。自动选择待处理消息、日周调度、适合真实个人记忆的模型准入、冻结中文质量集、事实自动接纳与长期性能仍未完成。普通MCP没有抽取/事实接纳工具；可信本地用户决定的事实接口见 [facts-v1](facts-v1.md)。
