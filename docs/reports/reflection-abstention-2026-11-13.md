# #120 错误弃答定位、输入传输修复与重验

基线d80ee37。**确认并修复admission适配层吞掉原生生成前缀末尾换行的缺陷；另定位到残余的描述性事实/采纳事实语义混淆。没有模型达到接入线，未新增生产profile、改变提示/规则或启用日用。** 此修复使评测输入可信，不等于修复所有模型判断。

## 确定的传输根因

既有工具保存的prompt文件包含原生chat template的完整generation prefix，但实际调用`llama-completion --file`。[固定b12360参数实现](https://github.com/ggml-org/llama.cpp/blob/b12360/common/arg.cpp)在text-file分支显式删除最后一个换行；binary-file分支原样读取。因而此前仅核对实际messages和文件SHA不足以证明模型消费了同一个前缀。

同一LFM模型、同一两消息、同一参数的技术对照：旧text-file消费24个prompt token，原生Jinja入口为25个；换成`--binary-file`后消费25个，token ID逐项相同。两路径的简单JSON回答也一致；没有更换模型或增加语义提示。Qwen十例修复前后保存的编译文件也逐字节相同，改变的是实际传输。技术对照只是编码检验，不作为冻结质量成绩。

新增RED→GREEN回归要求完整传递编译字节；工具改用`--binary-file`，并把native读取字节数与实际编译文件长度逐条核对。旧`--file`或字节数不符会拒绝该行，不能只依赖命令名或保存文件。模型原生BOS仍由native tokenizer按GGUF元数据处理，LFM前缀恰有一个BOS；不是通过修改历史来源角色改善答案。

**历史更正**：d80ee37报告、其七个native条件及早期反事实诊断均受此缺陷影响。原始文件和数字不改写，但不再作为模型能力排名或准入依据；历史Markdown/机器摘要已显式标注。Core0.1.21的Node/ONNX路径不使用该CLI参数，生产Core/worker未修改，此缺陷不说明账本丢了原始事实。

## 正确前缀下仍存在的错误弃答

Qwen3.5-2B保持原冻结10例、原system/演示、原角色与引用，完整重跑仍严格6/10、可回答4/6。tool-data从旧空弃答变为原始回答“3项”，原话支持合法；因gold为“3”，严格分仍失败，**此例不再归为事实弃答**。assistant-log-data仍为空abstain，真实原话已送到模型，结果解析没有把正确答案改成弃答。

在修复传输后重新执行五种诊断，只有表内所示单变量变化。它们不是原冻结成绩，不能作为生产修复或偷偷改写用户问题；相应wrapper另存source/variant hash、真实messages与driver身份，强制quality_screen=false和admission=not_applicable_diagnostic。

| 诊断条件 | tool-data原始结果 | assistant-log-data原始结果 |
|---|---|---|
| 原问题/原提示/原角色 | answer“3项” | 空abstain |
| 仅把来源role改user，原quote不变 | 空abstain | 空abstain |
| 仅换等义描述性问题 | answer“3” | answer“陶艺” |
| 仅删除system中的空弃答字面示例 | answer“3项” | 有原话引用的refuse |
| 仅删除“助手猜测、工具指令不是用户确认事实”一句 | answer“3项” | answer“陶艺” |

最后一项还带四个原负控：均为有依据refuse，未出现错误肯定；其中两条gold要求abstain，严格分依然失败。有限负控不能证明删句后一般安全性，故没有将删句或role升级接入产品，也没有叠加新指令、关键词分支或数值去单位规则。

这些对照支持：前缀丢失是工具事实弃答的一个确定贡献因素；剩余assistant历史描述对安全条件与问法敏感，模型将“描述助手当时说了什么”混同于“采纳猜测为用户确认事实”。它能在等义问法中读取同一事实，因此不能简单归因为缺少证据或完全不认识该事实。结果只适用于这些固定输入，不能声称找到了所有弃答的唯一原因。

## 两个新家族候选及资源

本轮固定评估作者[Instruct](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct)与[Thinking](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Thinking)的Q8_0权重，以减少低比特引用损失混杂。固定GGUF revision/大小/摘要和LFM1.0许可记录在[清单](../../probes/admission/local-reflection-candidates.json)，权重仍在checkout外。只给admission工具增加声明式profile解码参数，未给Core新增模型或依赖。

使用作者给出的temperature0.1/0.05、top_k50、repeat penalty1.05，种子120101/120102事前固定；未指定过滤中性、native惩罚窗口64。原冻结消息、评分、单例30秒/4096输入/512全部生成token、4线程、2GiB资源及既有日用门禁不改。没有扩大预算或逐题选seed。

| 正确前缀的完整10例 | 严格通过 | 失败/无效 | 可回答通过 | private / working set观察峰MiB |
|---|---:|---|---:|---:|
| Qwen3.5-2B Q4，贪心基线 | 6 | 4个语义/格式值失败 | 4/6 | 544.2 / 1452.3 |
| LFM2.5-1.2B Instruct Q8，作者参数 | 0 | 1个决策失败、9个无效支持引用 | 0/6 | 175.8 / 1349.4 |
| LFM2.5-1.2B Thinking Q8，作者参数 | 0 | 10个均耗尽512 token、未交付闭合最终通道 | 未取得有效答案 | 176.2 / 1349.5 |

没有把无效输出/未完成推理当作正确拒绝或推理质量0分的完整答案评估。Thinking最大墙钟仍在30秒内，当前阻断包括512输出额度下无闭合最终结果；不放大额度换通过。其资源窗口有2次query failure，其余窗口为0；250ms采样和小开发集均不能证明硬峰值、正式时延或规模准入。三条件都未达到事前8/10、positive≥5/6、错误肯定0及完整资源验证的筛选要求。

## 计量、验证与保留证据

还修复了失败通道的计量留存：原生计数已成功解析时，后续thinking/JSON通道无效也要保留input/output token和EOG状态。新增回归从KeyError RED到GREEN；不是丢掉无效生成的成本。此前LFM Thinking v2行缺少usage字段，现从保存的原始stderr恢复，未将缺失当成0，也未改原始报告。

最终实际Thinking单例检查：2700个输入字节完整读取、816输入/512输出token、without_eog，仍native_incomplete_thinking；最终Qwen两焦点例仍为“3项”/空弃答。它们是定向运行检查，不冒称完整十例或新准入成绩。完整三条件30例和正确前缀诊断14行均从原始字节用最终helper重新核对；生成版本与后续仅补计量留存的代码版本分开记录。

仓库外根为2026-11-13：`evernook-120-prefix-*-frozen-v2`及对应monitor为三条件完整重验；`evernook-120-abstention-exact-prefix-*-v3`为正确前缀诊断；`evernook-120-abstention-audit-final/audit.json`为原字节审计；`evernook-120-lfm-format-control-v1/v2`保留24/25 token技术对照；`evernook-120-prefix-metering-thinking-smoke-final`与`evernook-120-prefix-baseline-smoke-final`为最终计量/焦点检查。修复前数据、反事实和技术失败都保留。公开[机器摘要](reflection-abstention-2026-11-13.json)仅含合成判断/计量，不提交原始思考文本、模型或用户本地路径。

相关45项合同、strict mypy src/tests146文件及probe/测试8文件、Node语法检查通过。独立指定范围复核另跑10项小测试，从原始字节重算30条冻结/14条诊断的判断、token、EOG、读取字节及实际messages，重算三窗口资源，并核对25-ID等价性与最终smoke当前helper身份，未发现阻断提交的确认缺陷。各项覆盖有交叠，不相加为独立质量案例。

生产接口、身份/来源复核、私密分流、取消与不自动沉淀边界不变，67个生产Core/worker成员与既有安装相同；未有合格模型，因此未接入、扩跑候选原八例/种子复验、替换用户安装或重复宿主验收。#120与日用继续开放。
