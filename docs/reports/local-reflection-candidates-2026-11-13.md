# #120 本地模型与推理方案对照

> **后续更正（Task10）**：此报告的native试验使用`--file`，b12360会删掉prompt末尾换行，破坏原生generation prefix。原始数字与产物保留，但不能据此判定模型能力或作候选排名/准入；修复为`--binary-file`后另行完整复跑，见[弃答定位与重验](reflection-abstention-2026-11-13.md)。原Core Node/ONNX路径不使用该参数，不受此缺陷影响。下文为当时的受影响记录。

基线0d3eef9/Core0.1.21。本轮观察到GGUF/llama.cpp路线的资源改善，Qwen3.5-2B严格成绩从现有基线5/10达到6/10、错误肯定从4降到0；但可回答案例仅4/6，低于事前筛选线。**七个条件均未达到接入标准，没有新增生产后端或模型profile，#120保持open、反思与日用继续关闭。**

交付可复现的admission工具、固定资产清单、计量回归及完整失败记录。没有修改生产提示/示例、语义规则、Core/worker/dsh、原学习suite/gold/scorer/policy，也没有提高原输入/输出/时间预算。临时prompt和原生日志仅用于仓库外的合成评测，工具不能读取用户记忆或配置服务。

## 方法与筛选线

沿用冻结zh-reflection-semantics-v1的10例完整证据与原build_messages，cases SHA256仍为`eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0`。expected不进入模型输入；相同题目、原始角色/顺序和演示内容不变，仅使用各模型原生chat template编码，不新增语义提示。

筛选线在首轮结果之前写入计划及对话：严格raw至少8/10、可回答至少5/6、四个应弃答/拒绝例错误肯定0；无效/未完成输出不能算通过。模型及binary固定，CPU4线程、单例30秒、4096输入/512输出，context4608保留输入+输出空间且禁止context shift。接入筛选同时保留2GiB资源目标，除private bytes还看working set，不能借mmap隐去已映射权重。它只是开发筛选，不替代原正式日用质量、规模、时延与物理OS门禁。

先比较Qwen2.5-1.5B-Instruct、Qwen3-1.7B、Qwen3.5-2B的Q4 GGUF；仅对首轮较好的2B再测官方文本采样和原生thinking。随后根据明确记录的ruling追加一个4B低比特模型及原2B的Q6精度控制；没换语义消息、gold或筛选线，没逐题换seed或best-of。此为看过案例的自助开发诊断，不称盲测或严格外部预注册。

## 实际结果

下表为完整native输出按最终严格parser重新核对后的结果；每个条件均是相同10例，不是70条独立语料。错误肯定只统计可解析动作，无效JSON的语义未知，仍计失败，不能当作正确拒绝。详细数值、原始report摘要见[机器记录](local-reflection-candidates-2026-11-13.json)。内存单位MiB，表示250ms采样的owned进程树观察峰值；不能证明硬峰值。

| 模型/设置 | 严格通过 | 可回答通过/6 | 错误肯定 | 无效输出 | private / working set MiB |
|---|---:|---:|---:|---:|---:|
| Qwen2.5-1.5B Q4，贪心 | 2/10 | 0 | 0 | 2 | 354.7 / 1204.6 |
| Qwen3-1.7B Q4，贪心 | 4/10 | 2 | 0 | 0 | 729.1 / 1692.8 |
| Qwen3.5-2B Q4，贪心 | 6/10 | 4 | 0 | 0 | 542.7 / 1450.6 |
| Qwen3.5-2B Q4，官方文本非思考采样 | 2/10 | 0 | 0 | 2 | 542.9 / 1451.3 |
| Qwen3.5-2B Q4，原生thinking文本采样 | 5/10 | 3 | 0 | 2 | 543.2 / 1451.1 |
| Qwen3.5-4B UD-IQ2_M，贪心 | 4/10 | 4 | 0 | 1 | 739.4 / 2035.5 |
| Qwen3.5-2B Q6，贪心 | 6/10 | 4 | 0 | 0 | 543.7 / 1731.9 |

Qwen3.5-2B贪心能回答三个多跳目标，并拒绝历史工具/用户话语授权；但tool-data和assistant-log-data仍弃答。Q4的assistant-guess为有依据refuse而gold要求abstain，reported-guess缺理由引用；Q6也未补足两个可回答案例。拒绝/弃答中的安全标签差异单列解释，严格分数不放宽。所有条件错误肯定0包含很多拒答，不能等同于语义能力全面提高。

4B低位宽能回答一些局部事实，却出现多跳漏引与改写原话；体量更大没有得到更好的严格成绩。其working set离2GiB仅约12MiB，更不能由一次采样颁发资源准入。Q6与同版本2B Q4相比只增加量化精度，仍为6/10；本轮没有证据支持继续增大权重或调整模板即可解决问题。

thinking条件的10例均直接生成关闭通道，拆分前reasoning_chars=0；全部生成token仍计量。没有观测到非空思考，不能把模板开关当作有效推理。该监控窗口另有1次进程采样query failure，保留为覆盖缺口，未重跑挑选更低峰值。

## 固定资产与推理边界

使用官方[llama.cpp b12360 CPU发行包](https://github.com/ggml-org/llama.cpp/releases/tag/b12360)，实际版本1.0.0-dev/build12360/commit2d8a32502；这是预发行构建。zip SHA256为`eae1726047e0eaa707b1302929d196c999b96c2f243b21aea6e93bcebacc4e17`。新工具逐一验证解压成员，并拒绝额外DLL/EXE；模型大小/摘要、renderer、Jinja、Node、scorer/protocol与采集器身份一并记录。

模型来源、固定revision和文件摘要见[资产清单](../../probes/admission/local-reflection-candidates.json)。Qwen2.5来自Qwen自己的GGUF仓库，其余为Unsloth转换/量化，不称官方原件；4B的IQ2_M与2B的Q6也不能直接当作只改变参数量的控制。模型、运行库均在checkout外，未新增项目依赖、下载器、监听服务、云回退或Core配置选项。

[Qwen3.5-2B模型说明](https://huggingface.co/Qwen/Qwen3.5-2B)说明默认非思考，所测两个附加设置按本次读取的文本任务建议：非思考temp1/top_p1/top_k20/min_p0/presence2，thinking temp1/top_p.95/top_k20/min_p0/presence1.5，repeat penalty均1；固定seed分别12091/12092。llama.cpp惩罚窗口显式为整个4608 context，不使用旧版本不接受的-1。未使用通用JSON grammar或按来源类型代答的规则。

原生日志会逐token记录remaining计数，包含EOG和任何思考token。stderr中原生token片段可能不是完整UTF-8，因此最终工具按带原生日志前缀、连续递减的ASCII计数解析；stdout仍严格UTF-8。只移除CLI自身终止标记，思考通道只允许唯一原生关闭分隔符；不截取JSON子串、补括号、换ID或改写引文。共用judge_generated只负责语义计分，不伪造旧ONNX envelope的network_attempts。子进程使用明确的offline/local参数并清除环境LLAMA_ARG_*与HF_TOKEN，但未测网络attempt数，不能宣称“联网尝试0”或完整OS沙箱。

## 验证与证据

三个早期技术问题均保留：renderer首次用Windows盘符直接作ESM URL而失败；原型v1将含不完整token片段的stderr整体按UTF-8解码，两个模型在首例后采集失败；附加设置v3使用旧语义repeat-last-n=-1被新CLI拒绝。分别修正为file URL、二进制日志计量、明确4608窗口后完整重跑，没有把这些中止记录重新标成模型质量通过。

最终仓库工具对2B Q4另完整运行十例，仍6通过/4失败、可回答4/6、错误肯定0、quality_screen=false、collector_unchanged=true；owned private peak569356288字节、working set peak1522229248字节，186采样/query failures0。最终report SHA256=`ea77335edbdda977ac2a14494e77aee42ee902220ead929d48bafb754884407b`。启动资产核验在逐例计时之外；最终工具计时包含模板渲染，早期原型逐例仅native调用。OS文件缓存未控制，不能据此称正式冷/热性能达标。

仓库外证据根为2026-11-13：`evernook-120-llama-*-v2/v4/v5/v6`及各自monitor目录保存完整输入、原始字节、命令和计量；`evernook-120-local-candidate-audit-final/comparison.json`保存全部70个已完成native输出的最终parser离线核对，未改原报告；`evernook-120-local-collector-qwen35-final`及对应monitor为最终工具十例。新工具、清单与[运行说明](../../probes/admission/local-reflection.md)可重现，无需依赖未提交的原型脚本。

新增计量测试从缺模块RED到实现GREEN，覆盖二进制token日志、断裂/伪造计数、无JSON修补、未闭合/空思考通道；资产测试覆盖模板原样保留、截断GGUF、模型/二进制篡改与额外DLL。相关40项、strict mypy src/tests145文件及probe/测试6文件、Node语法检查通过。独立指定范围复核未发现确认缺陷：从保存的字节重算全部70例及最终工具10例的消息/计量/判断，核对资产/报告/代码身份与186采样资源，并独立运行21项相关合同测试及Node检查；未启动新模型或修改文件。各测试和诊断有交叠，不相加为独立覆盖。

原zh-learning-v1及语义集的gold/scorer/policy保持不变；新工具没有候选通过筛选，因此未扩跑候选原八例、重复种子或安装/服务/dsh/取消接线验收，也未重建或替换既有Core/用户安装。下一步仍是找到质量与资源均合格的模型方案；现有身份、来源、私密、取消和日用门禁没有改变。
