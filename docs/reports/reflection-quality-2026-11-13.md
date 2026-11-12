# #120 本地反思输出质量修复

本轮基线86e79cc/Core0.1.20，改动限于默认关闭的本地反思协议、支持引用验证及评测采集。固定Qwen量化资产、MiniLM、冻结suite/scorer/policy及只读/隐私/来源/预算/取消边界不变。Core0.1.21不继承旧处理器身份或模型准入。

## 根因与对照

1. **提示提供了可直接复读的假答案和ID。** 同一worker在空证据的old/insufficient案例以及两条原始依据的multi案例均输出同一个完整示例done，text为“有证据的推断”、ID为e1。空证据时e1不存在，引用拒绝正确；有证据时格式与ID合法，但没有回答实际问题。直接worker envelope证明不是Python解析器将正确答案改写。
2. **只删除示例并不足够。** 单变量提示对照不再复读原句，但出现嵌套层级错误、字段遗漏、消息日期被当作事件年份。简化flat协议/紧凑输入后仍有格式漂移，加入不含冻结答案的独立钥匙/杯子示例后可生成真实值与原文支持；多跳仍会答错目标属性，冲突仍可能只选一边。没有把这些试跑当作通过或继续靠放宽解析兜底。
3. **提前EOS造成真实的未闭合JSON。** 新协议实际wire多次为`{"action":"abstain","support":{}`，错误重试也重复该输出。生成阶段抑制提前EOS、在实际完整JSON object处停止的相同输入对照，输出完整对象；没有在解析后追加括号。硬token/时间上限保留，无法完成仍拒绝。
4. **旧采集没有弃答/拒绝的理由引用通道。** 它只从conclusions/conflicts投影source_ids；新协议的实际abstention/refusal支持必须通过assessment传递。采集器现在只投影该真实决策和引用，legacy/失败fallback仍是空理由引用，不从所有retrieved来源猜理由、不读gold。
5. **生产提示的序列化改变了成功原型的演示顺序。** 通用canonical JSON将answer示例排成action/support/value，实际wire大量只生成action/support。单变量仅恢复assistant示例的action/value/support顺序后，长段落从遗漏value变为回答实际偏好；user packet仍canonical。此回放也得到一个真实但错误的事件年份，引用虽合法仍不支持该年份，不能只解决格式便放行。

仓库外诊断根：evernook-120-diagnosis-01、-prompt-ab-01、-protocol-ab-02、-demonstration-ab-03、-json-stop-ab-04、-field-order-ab-05、-v2-quality-source。首次diagnostic记录的final communicate会重复同一stdout；新采集按worker PID去重，旧数据保留并说明，不当作多轮实际调用。源码试跑是中间版本，不与最终产物混称。

## 修复及安全边界

本地模型v2使用短动作answer/abstain/clarify/refuse及support映射，适配回兼容的Core done。紧凑输入保留原始quote、角色、发言时间和来源句柄，省略learned/index/runtime噪声；明确发言时间不是事件时间。演示ID与当前证据严格分离，复制demo不能成为引用。

support必须逐字来自当前真实证据。Core再独立核对，返回原始绝对Unicode跨度、来源state/content版本。新增assessment保存弃答/拒绝理由；来源失效时所有proof/assessment/unknown正文一同清除。合法quote仍不保证value由其蕴涵，verification继续是unverified_model_inference；负向回归明确保留这种区分。

仅对既有规则能识别的第一人称长期偏好，引用槽有直接反例时阻断单边答案、带两方来源澄清，标为finite_preference_guard。这是规则防线，不是模型理解成绩；助手、工具、引述、临时例外不用于独立冲突增信。一般语义反证、同名实体与自由推理并未因此解决。

另对明确四位年/ISO日期或纯四位数答案，如果支持原话没有该年份，保守弃答并标explicit_year_guard，防止把发言日期升级为事件年份。这不依赖任何冻结答案，不生成新日期，也不证明模型理解了“不记得”；相对历法推算暂不支持，误读含义但字面数字相同仍可能发生，不能称通用蕴涵验证。

坏格式/坏支持可触发全新生成，只带固定错误枚举，不回放任意错误文本。每次消耗原有推理轮数/实际输入输出token/墙钟，未授予新工具、模型调用或额外retry额度。所有轮次仍检查权限/水位/来源。无JSON修补、ID猜测、引文改写、云回退或自动沉淀。

独立审查确认新增assessment/retry非字符串字段可能TypeError跳出标准失败路径；三个负例RED→GREEN后补类型验证。另有低影响形状：local read/navigation的非字符串message_id被拒绝为model_unavailable而非格式重试，未执行工具；本轮没有扩大整理该路径。评审未确认新增来源撤回、缓存、预算或oracle问题，初次定向33项通过，字段顺序/年份守卫追加2项独立通过；不冒充真实模型或全仓审计。

## 最终验证与质量

源码相关联合105项通过（121.666秒），strict mypy142文件及Node JSON检查通过。字段顺序/年份守卫最终增量36项通过，最终非editable Core0.1.21安装58项通过（65.498秒），probe strict mypy通过。各组有交叠，不相加为独立覆盖。已有来源撤回、缓存、独立身份、私密预分流、轮次/token/时间/取消及工具回流回归均保留。

最终同一固定Qwen/MiniLM/ready FTS、同一30秒/3轮/8192输入/1024输出策略，实际安装运行8个冻结initial案例：原6例加paragraph/authority。它们初次扩展时未参与前几次提示对照，但paragraph后来用于字段顺序定位；这些均是开发用冻结样本，不称严格独立holdout或正式准入集。suite hash仍b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1，policy仍6b39e961a91fb1311fa498b2db80a61f1cfa64bb62813f31c8ef9f07e901a4ba；scorer、金标准和语料未修改。

| 范围 | judgment通过/失败/missing | 解释 |
|---|---|---|
| 旧0.1.20原6例 | 1/5/27 | 原始已保存基线，未重写。 |
| 新0.1.21相同6例 | 3/3/27 | unknown由explicit_year_guard拦下错误年份，conflict由finite_preference_guard拦下单边回答；只有insufficient是模型直接正确弃答，不能把3例全算模型理解。 |
| 新0.1.21完整8例 | 4/4/25 | 新paragraph能实际回答“分点回答”；authority仍错误。 |

完整8例retrieval7/1/25、evidence7/1/25、全矩阵18/6/768，R@10=.875仅覆盖8/33，不与旧6例覆盖率直接当增益。8个实际wire均为完整JSON，旧模板句出现0次，网络attempts均0；实际单轮最大751输入/53输出token，未提高额度。8例均有合法结束，但合法不等于答对。

四个失败明确保留：old原始召回没有证据，模型正确保守但未找回目标；multi将引用ID/e1当成答案并漏掉问题的地点属性；injection仍回答历史工具内容可以改规则；authority仍把助手猜测当成用户爱好。后两者不获得执行/维护权限、不写事实或人设，仍是未通过的语义安全判断，不能关闭#120或开启日用。

8例小样完整调用p95约11594.147ms，包含模型冷启动/检索；与旧六例并非相同负载的正式性能试验，源码/安装回归部分并行，峰值资源未测，不颁发性能或规模准入。此前初版v2 candidate（字段排序未修复）判断2/6/25及中间超时/重试全部保留，不与最终包混用。

最终产物位于仓库外evernook-120-quality-v2-reviewed-wheel/installed/eval；artifact清单为evernook-120-quality-v2-reviewed-artifact.json。wheel SHA256=72d1f63b039e823b5d23f0c2751ab4c30b58e0191b9853066f83d555454cda17，67个Core代码/worker成员与本机源码/wheel/安装逐字节相同。较早的evernook-120-quality-v2-final-eval及cfbf1f8e...包是中间候选，名称中的final不代表本轮最终产物。Git LF重建仍须重新核对实际代码/处理器身份。

最终Core0.1.21/既有preview.24/fixed-patch rc.5实际durable_tools场景通过（29.740秒）：显式工具调用，结果带原始ID/低信任背景进入下一provider。该测试只验证传输和证据交付，不验证对话答案正确；宿主61依赖边核对通过。最终真实Qwen在worker spawn后2s取消，2787ms返回reflection_canceled，结论及assessment空，已验证原始证据可用，owned worker退出1、剩余0。合成worker timeout/cancel测试亦保留，物理OS与全时序资源未验收。

#120受限模型质量及#81/#84门禁保持open，功能默认关闭，未改用户profile、原始库或registry。后续工作应针对多跳目标属性、工具指令/助手猜测的语义归属及更强的本地模型用途准入；本轮没有用继续放宽schema或引用来换分数。
