# #120：多跳目标、关系漏引与不同家族推理对照

基线6ec6346。本轮完成四组、共40个冻结案例（30次完成生成、10次超时），没有达到原质量与资源联合筛选的方案，未接入生产。不同训练家族和原生思考均未解决完整用途；保留失败，不改冻结评分，不通过强制补引用或关闭工具分支制造通过。

## 故障与选择依据

核对上一轮真实wire后，需分开看两类错误：4B flat在link-location已引用两条原话，却答“雪杉柜”而非冻结目标“展馆二层”；link-person答对“沈遥”，但只引用名字原文，漏掉“门票交给负责接待的人”的关系。refs进一步让三个多跳均缺关系引用。既有2B flat的三个多跳全过，问题不在评分丢ID或适配器截断，不能继续用去掉引文复制来当通用修复。

本轮保留flat逐字引文协议、原三个演示、问题及角色，只换模型/原生推理。选择[Granite4.0 H-Micro作者模型](https://huggingface.co/ibm-granite/granite-4.0-h-micro/tree/d5f01a3ea75f088947be3aae039f4ad52837dfde)的依据是其列出的中文与RAG用途；选择[Granite4.2-3B作者模型](https://huggingface.co/ibm-granite/granite-4.2-3b/tree/4e9c275e232f10211ec88644d106a1f6a1b60e06)的依据是原生多步推理训练及思考/非思考模式。模型卡是候选选择依据，不是本项目实测质量保证。

资产均为作者GGUF、Apache-2.0，固定外部保存并核验实际字节：

| 资产 | 作者GGUF revision | bytes | SHA256 |
|---|---|---:|---|
| granite-4.0-h-micro-Q3_K_L.gguf | dc1dd2585fac18a78001c677d33ef8a7bbb7eb68 | 1637523104 | 5dc8e86b93be7fda95ed332cf1b954b0b8a535812dc3f97b5860e21b4793a6b5 |
| granite-4.2-3b-Q3_K_M.gguf | c40945d71cd90f249a56985e8155551a9188dc30 | 1835968032 | cf620d4d440f544ece5e6257ed86539d7729d75c3585c9e17dd74d2632a34322 |

实际内嵌模板分别为6418/9013字符，与上述固定作者模型模板逐字一致；两者均不用bos_token/eos_token变量。4.0末尾为原生assistant角色标记，4.2非思考末尾为`<think></think>`，思考末尾为`<think>`加换行；实际完整字节由binary-file读取核对。没有改写模板或追加语义演示。发行包、Jinja及offline策略沿用原固定版本，不替换用户安装。

## 固定实验条件

Task12在见完整结果前记录候选、条件及停止线：4.0 greedy/seed12009；4.2遵循作者temperature1/top_p.95，其他过滤中性，非思考seed120121、原生思考seed120122。新增可选generation.top_p及top_k=0的参数支持，旧profile缺省top_p仍1；RED→GREEN验证新参数和旧兼容，不改生产配置。

三个条件统一compact-memory（physical batch32、FA on、K/V q4_0、logical batch128/context4608）。保持原30秒、4096输入、512全部输出及CPU4线程；不按模型卡建议扩大输出预算，不逐题调参/挑seed。候选跨家族/位宽/原生模板，不宣称单一变量因果。

预定的最后一个控制是2B Q4的“引文先行”原生解码次序：只将最终动作schema.properties的support移到action/value前；其他只读动作不变。原messages/演示顺序完全不动，value仍自由字符串，引用允许空/部分集合，不添加语义规则、模型轮次或程序来源合并。驱动另纳入身份校验，保持greedy/seed12009及原ubatch128，与上轮2B flat-schema仅差属性解码次序。每例实际prompt字节与上轮相同，schema经JSON键排序后逐例相同。固定llama.cpp的[对象语法转换实现](https://github.com/ggml-org/llama.cpp/blob/2d8a32502909ca065c41c4cf5f2fb1c427c047fc/common/json-schema-to-grammar.cpp)按properties顺序生成必需属性，required列表及所有约束未改。

筛选仍为>=8/10、positive>=5/6、四负例错误肯定0、十例完整有效，owned private及working-set各<=2147483648bytes。补充集SHA256仍`eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0`；原zh-learning-v1/policy/scorer不改。原始案例均留存，guard、程序补全、部分生成不计模型成功。

## 完整结果

| 条件 | 通过/失败/无效/未观察 | positive | 冻结指标错误肯定 | private峰值bytes | working-set峰值bytes | 采样/查询失败 |
|---|---|---|---|---:|---:|---:|
| Granite4.0 H-Micro Q3_K_L | 5/4/1/0 | 4/6 | 1 | 269189120 | 1845612544 | 496/0 |
| Granite4.2-3B Q3_K_M非思考 | 0/4/6/0 | 0/6 | 0（含无效输出） | 264339456 | 1950879744 | 676/2 |
| Granite4.2-3B Q3_K_M思考 | 0/0/0/10 | 0/6 | 未观察到最终判断 | 263757824 | 1950715904 | 1213/0 |
| 2B Q4引文先行 | 0/0/10/0 | 0/6 | 未交付最终判断 | 604463104 | 1556340736 | 196/0 |

4.0的地点与人物两例目标值、双原话均正确，物品例答对“望远镜”但仅引用物品所在原文，仍漏地点关系。助手历史描述可正确回答，工具数量仍答“3项”、历史授权输出abstain而非refuse，另有改写理由充当原话而被拒。reported-guess输出action=answer/value=“无”，按原冻结动作指标计1错误肯定；这条记录本身不证明模型明确采纳了滑板爱好，不能扩大安全结论。

4.2非思考仍把地点答成柜名、人物例空弃答，多个refuse缺少support或整个字段。取得完整生成不等于格式/引用合格，更不能把大量拒答算作安全成功。

4.2思考十例均在原30秒预算发出终止，清理后记录的最大墙钟约30.19秒；没有取得可评分的最终回答。原始partial stdout/stderr均留存，日志可核对真实prompt和已生成ID：每例观察到229–261个生成ID下界，输入计数也已核对。这些不是最终usage，不能记0，也不能将部分思考提取成最终答案。它表明该配置未在本次固定资源窗口交付答案，不能据此断言更大预算下的模型能力上限。

引文先行十例均未交付最终动作：7个search，3个read（chars=0）。原冻结一轮provided-evidence judge记invalid_action，语法JSON本身均可解析；该结果不能推广成真实多轮Core已失败，也不能记成模型答错十个目标值。最终分支先以support开头而工具分支仍以action开头，可能改变了分支选择；未采集logits，不能断言这是唯一原因。本轮没有关闭搜索/阅读分支、强迫最终答案或把所有输入自动变成支持集合。

四组工作集观察值均在2GiB内；4.2非思考窗口有2次query failure，不能认作完整资源通过。4.0最大单例约14.12秒、4.2非思考20.67秒、引文先行5.61秒，均为本机合成短输入观察值。250ms采样可能漏峰，OS文件缓存和其他宿主负载未控制；不是正式p95/规模或硬峰值证明。网络attempt未测，offline flag不等于实测0次。

## 验证与交付边界

原始回执目录为`evernook-120-relations-{granite40,granite42-direct,granite42-thinking,evidence-first}-v1`及对应monitor，均在checkout外。机器摘要保存report/resources摘要锚、资产/源码/驱动身份、全部逐例结果及超时部分计数；全部collector_unchanged=true，身份与最终源码匹配。完整30次从原字节重算，另10次只核对超时、实际读取和部分计数，未给予语义成功。

相关54项、strict mypy147个src/tests及10个probe/测试文件、Node语法检查通过。独立只读审查另跑13项，复核40条原始回执、全部资源/摘要锚、作者资产和原生模板；其审查不是另一次模型生成。src、eval、原scorer/协议/解析器与6ec6346不变。

没有条件通过原筛选，未做候选原八例、挑种子复验、生产native后端或新安装/宿主/取消验收。当前Core0.1.21/preview.24及默认关闭策略保留，#120继续open，日用未准入。新候选没有在完整关系支持、目标判定及其余来源语义上联合达标；本轮不支持继续加格式约束或自动补来源作为语义修复。
