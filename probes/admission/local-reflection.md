# 本地模型/后端诊断（#120）

`local_reflection.py`仅用于Windows上的冻结合成语义诊断，**不是Core反思后端**。不接触记忆库、真实对话、dsh或普通上下文；临时prompt和verbose日志都可能含完整输入，因此只能使用随仓库冻结的合成集。资产、产物必须置于checkout外。

固定消息、expected隔离与严格计分复用zh-reflection-semantics-v1；模型自己的GGUF chat template只编码角色边界，不追加提示、示例或规则。当前所有候选均未通过预先的接入筛选，也未获得日用准入。

## 准备

1. 按`local-reflection-candidates.json`的固定URL手动下载模型及llama.cpp CPU archive，核对SHA256。工具不下载、不升级依赖或自动配置Core。
2. 将archive放在资产根目录；在其`llama-b12360`子目录完整解压发行包。模型按清单`file`命名放在根目录。启动前工具会验证archive摘要、解压成员原始字节、额外DLL/EXE、模型大小/摘要。
3. 使用现有Node24及外部`@huggingface/jinja@0.5.11`目录。可以复用既有transformers4.2.0资产中的依赖；工具固定验证`dist/index.js`摘要。没有给本项目添加npm/Python生产依赖。各候选的量化来源见清单：Qwen2.5和LFM来自作者GGUF，其余为第三方Unsloth量化；LFM为LFM1.0许可，不混称Apache原件或自动分发权重。

## 运行

从仓库根目录运行，使用新的输出目录。下例的路径应替换为真实外部目录；monitor负责250ms的owned进程树采样，两个结果目录不能已存在。

```powershell
$env:PYTHONPATH='src;.;probes/admission'
$nativeProbePython=(Resolve-Path .venv/Scripts/python.exe).Path
.venv/Scripts/python.exe -B probes/admission/monitor.py --output C:/outside/new-monitor --max-seconds 360 -- $nativeProbePython -B probes/admission/local_reflection.py --assets C:/outside/assets --profile qwen35-2b-q4 --node 'C:/Program Files/nodejs/node.exe' --jinja C:/outside/node_modules/@huggingface/jinja --output C:/outside/new-run
```

`--mode greedy`默认使用非思考/贪心；两个预先记录的2B对照为`--mode sampling --seed 12091`、`--mode thinking --seed 12092`。不得逐题换seed挑成功输出。thinking只在唯一的原生`</think>`处拆分最终通道，全部生成token仍消耗512上限；reasoning_chars=0明确表示空通道，不能当作发生了有效思考。

LFM使用`--mode profile --seed 120101`（Instruct）或`120102`（Thinking）；其声明的temperature0.1/0.05、top_k50、repeat penalty1.05从固定清单读取，不套用Qwen参数。未指定的过滤显式中性，惩罚窗口为llama.cpp默认64。新profile仍仅为admission资产描述，不是Core可配置模型。

每例完整渲染+执行30秒，4096输入/512输出，4 CPU线程；context4608预留输入与输出，禁止context shift。启动资产核验在逐例计时之外。新工具总计时含渲染；早期外部原型逐例计时仅覆盖native调用，比较时应区分。环境中的LLAMA_ARG_*与HF_TOKEN不传给子进程，原生`--offline`和本地路径阻止自动下载；没有实测网络attempt计数，不能宣称“联网尝试0”。

## 产物与边界

每例保存实际messages、编译prompt及其摘要、native命令、原始stdout/stderr、实际token/结束原因和严格判断。必须用`--binary-file`完整传递生成前缀；b12360的`--file`会删掉末尾换行，旧记录受此缺陷影响，不用于能力排名。工具还核对native实际读取字节数与编译文件一致，不能只保存文件SHA便假定模型读到了相同输入。stderr中token片段可能不是完整UTF-8，计量仅解析带原生日志前缀且逐步递减的ASCII计数；不重分词估数。通道解析失败也保留已取得的token和native EOG状态。只移除native终止标记，不抽取JSON子串、补括号、替换ID或改写引文。规则前生成文本直接经judge_generated计分，不伪造旧ONNX worker的network_attempts字段。

报告的quality_screen只表示完整有效输出至少8/10、可回答案例至少5/6、错误肯定0；资源仍须读独立monitor结果，同时查看owned private bytes与working set，mmap模型文件大小另列。采样不能证明硬峰值；任何query failure都保留，不能据低private bytes忽略已映射权重。admission始终pending，CLI没有启用profile、写画像或授予工具权限的路径。

Task11增加三个彼此独立的诊断开关，默认全部关闭：`--wire refs`将模型support改为证据ID数组，仅替换原格式说明及原三个演示的support字段；适配器只绑定模型所选ID的完整当前quote，不补引用、不改decision/value，不截短超过现有2048跨度限制的原文。`--constrained`把请求的结构schema交给原生JSON-schema解码；value仍为自由字符串，support允许空集合，全部只读search/read/navigation及已允许view动作仍可选，缺证据仍由原冻结评分失败。schema不读取gold、问题或来源角色，不强制引用所有输入。保存原始final_text、机械projected_text和schema摘要，不能把绑定的原文算作模型逐字复制能力。

`--compact-memory`仅调整CPU执行：physical batch32（原128）、flash-attention on、K/V q4_0；logical batch128、context4608、4线程、512全部输出和单例30秒不变。实测输入超过4096会被明确标为预算失败（诊断的后验计量，不是生产预检）。较强权重与此设置的比较包含两个变量，不宣称单变量模型能力结论。新IQ3权重仍为固定第三方量化，清单不构成Core profile或自动下载配置。

最新完整结果与失败边界见[弃答定位与正确前缀重验](../../docs/reports/reflection-abstention-2026-11-13.md)；[早期记录](../../docs/reports/local-reflection-candidates-2026-11-13.md)保留传输缺陷更正。没有候选通过筛选时，不继续接入或重复宿主/安装验收。

## 弃答因果诊断

`abstention_diagnostics.py`显式提供baseline、role_user、descriptive_question、remove_empty_example、remove_source_trust_sentence五种反事实对照。它们只选择两个焦点例，最后一种附带四个负控；可能修改source角色、问题或删除系统片段，**不是原冻结评测**，不能将改角色当修复。wrapper保留原始expectation和source cases hash，另记录实际变体hash/driver身份，强制quality_screen=false及admission=not_applicable_diagnostic。不向模型提供gold，不生成新提示或示例，不接入Core。

使用同一组外部参数，将入口换为`probes/admission/abstention_diagnostics.py --variant baseline`即可。所有实际输入与输出仍保存；诊断被hook的源码文件未变不代表输入不变，应查看明确的variant及消息记录。更正和正确前缀下的结果见[弃答定位报告](../../docs/reports/reflection-abstention-2026-11-13.md)。
