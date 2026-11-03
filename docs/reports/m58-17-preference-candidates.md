# M58-17：离线有来源偏好候选

对应 [#75](https://github.com/destinyTaoTao/evernook/issues/75)。实现显式维护入口，复用固定本地模型、候选租约、来源版本与原文校验。三类标签保存在原候选草案中，旧无标签 payload 保持兼容；不修改事实/画像或启用自动接纳。协议见[preferences-v1](../contracts/preferences-v1.md)。

首版采用有限的完整陈述形式，拒绝把片段从否定、引用、角色扮演或混合高风险整条来源中剥离。它是保守低召回策略，不是已通过质量准入的语义分类器。未识别内容留在通用原文/抽取审查路径。

5 项公开接口回归覆盖三类及原文坐标/版本/重启读取/重复任务、12 类排除样例、上下文剥离/非用户/争议、排版与带版本工具，以及真实 CLI 缺失资产失败和原文可读。既有候选 12 项与抽取 9 项回归均通过；mypy src、测试和探针共38文件通过。最终全量 Python 契约回归 **344/344** 通过，耗时735.435秒，仓库外日志 `C:\Users\dengtaotao\Documents\Codex\2026-11-04\evernook-m75-python-full.log`。

Standards：0 项文档规范违反，0 项需修复的代码异味；额外核对了新探针与真实测量。Spec：最初缺少实际后端质量/资源证据，补齐下述真实模型逐项结果后，该项消除，最终0项未解决发现。独立审查核对了结果/探针/策略哈希、实际持久化的5个候选及来源版本；峰值内存或正式准入不属于本次已验证结论。

16 项独立预标注策略探针为7个可识别偏好与9个排除/未知样例；确定性 selector 不执行模型，只测策略与提交。最终独立任务复跑 16/16 匹配，16 次 process 调用总耗时 0.320658 秒。该小样本不能推断正式冻结集分数或性能分位数。

## 真实本地模型测量

初始机器仅检测到 C 盘，历史 E 盘模型路径不可用。按维护者自主推进授权，在仓库外显式在线准备项目原先锁定的同一 `onnx-community/Qwen3-0.6B-ONNX` revision `58ca03a5e7b354b480b1a82b34bde8dc7b854cc0`，复用既有准备脚本、每次推理核对九个固定文件哈希；没有更换模型或向运行入口添加下载器。依赖为 Node 24.12.0、Transformers 4.2.0、ORT 1.24.3，CPU、每次新建模型进程。运行时继续禁止 fetch/socket、local_files_only；未声称系统断网验收。

2026-11-04 实跑16个独立场景：**14/16 匹配**，7个预期偏好中5个落地，三类均有真实来源草案；9个排除/未知场景均未产生偏好。case1合法任务未产生预期候选，case4模型输出无效被阻断；均计为失败，不改预期或修补输出。全部5个结果仍为 candidate，没有事实/画像自动接纳，quality_admission 仍 pending。该模型尚不适合据此启用日常自动沉淀。

| case（从0开始） | 预期 | 实际 | 错误 | process 秒 |
|---|---|---|---|---|
| 0 | response_style | response_style | none | 7.164 |
| 1 | response_style | None | none | 4.864 |
| 2 | response_style | response_style | none | 6.945 |
| 3 | technical_tool | technical_tool | none | 6.898 |
| 4 | technical_tool | None | extraction_invalid_output | 7.224 |
| 5 | general_interest | general_interest | none | 7.245 |
| 6 | general_interest | general_interest | none | 6.669 |
| 7 | None | None | none | 6.810 |
| 8 | None | None | none | 7.057 |
| 9 | None | None | none | 6.633 |
| 10 | None | None | none | 7.797 |
| 11 | None | None | none | 7.213 |
| 12 | None | None | none | 6.613 |
| 13 | None | None | none | 7.253 |
| 14 | None | None | none | 7.025 |
| 15 | None | None | none | 7.470 |

总 process 墙钟 **110.880751 秒**，包括各次模型文件校验、进程启动/加载、选择与提交，不含在线准备、运行库初始化和原文录入；不是纯推理或全命令耗时。环境 Windows11 / AMD64、Python3.14.4、CPU 标识 Intel64 Family6 Model183 Stepping1。测试套件同期运行，该测量不是独占机器的容量或p95基线。峰值模型内存未测，保留 null，不宣称正式模型/硬件准入。

真实结果目录 `C:\Users\dengtaotao\Documents\Codex\2026-11-04\evernook-m75-qwen-probe-01`，report.json SHA-256 `867697259ece7573b0d6715c8764332808ea07b1a61a8ad2e93673c58032c925`，探针 SHA-256 `7eca08f6b630d3683878b75ca371129bd5e8c865b0e19c599e633b7864573427`。backend 身份 `qwen3-0.6b-extractive-v1:fde9a9ca7afa197781f897fc9cdc9d88b5c3d75c4c7f9600bb88e02af7e08d79`；策略 SHA-256 `e43bfad752ea7d06069a963b078348b785fd0a51ccc1c6f23c8fdecaee73a41b`。资产及配置均在同日仓库外 `evernook-m75-model-assets`，不提交权重、配置和运行库。

CLI 对缺失资产给出 extraction_unavailable，不丢失原始证据。此路径和实际推理失败都不会静默下载或退云；宿主回答、完整冻结集、模型内存与长期运行仍需后续独立验收。
