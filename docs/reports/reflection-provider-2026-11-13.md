# #120：路由根因控制与显式反思provider

基线df0eaba。工作始于2026-11-13，最终验证于2026-11-14（Asia/Shanghai）。本轮先完成26次本地反事实诊断，再按用户追加授权交付可配置的本地/线上provider基础，Core0.1.22、SDK0.1.1、dsh preview.25。SDK、认证服务API、CLI和MCP复用同一证据与输出校验。默认本地、实验反思默认关闭，不自动回退云端；没有选择或调用真实付费模型，#120和日用质量门禁仍开放。

## 根因：语法分支影响路由，引用不足不能都等同于事实幻觉

固定2B Q4、同一冻结消息/贪心seed12009，补两个完整十例控制：只在诊断中删除工具分支，分别保留原字段顺序及引文先行。原始问题、角色、演示和严格judge不改，value仍为自由字符串，没有自动补引用。这种受限动作环境不是生产策略或候选准入，机器回执始终quality_screen=false/admission=not_applicable_counterfactual。

| 条件 | 有工具分支（既有结果） | final-only控制（本轮） |
|---|---|---|
| 原action/value/support顺序 | 6通过/4失败 | 6通过/4失败，十条严格判断与原基线相同 |
| support/action/value顺序 | 7 search/3 read，无最终判断 | 2通过/6失败/2无效 |

移除工具分支后，引文先行不再转向工具，但仍仅2/10：空支持弃答/拒绝很多，地点答成“展馆二层雪杉柜”也未匹配冻结目标。该控制支持“不同分支首字段/语法可行路径影响了路由”的解释，不能证明这是唯一原因，更不能据此关闭生产工具分支或宣称恢复了推理能力。原格式未改善，亦未出现通用质量修复。

另对既有4B IQ3的三多跳逐条删除一个来源，共六例，原证据ID/角色保持，不重编号，不提供gold给模型。人物例删掉“门票交给接待人”后仍凭名字原文答“沈遥”；删掉名字原文才弃答。这支持“依赖直接答案线索、未充分检验桥接前提”的有限判断，解释了为何有名字值却缺关系来源。物品例删掉地点原文后仍答“望远镜”，但问题本身已包含柜子/地点关系，不能把这一条单独当作不理解多跳的证据。地点两种删除下都答柜名，显示粒度/锚点选择未遵循冻结目标；柜名在自然语言中也可描述地点，不等同于凭空捏造。

上述六例沿用原expected，所以缺少任一必需来源必然不能得原严格分；0/6不是新语义准确率。原gold、评分和标签均不改，不能用归一化、引用集合合并或角色升级掩盖失败。三组资源样本187/183/296，query failure均0；4B工作集仍超过2GiB。所有原stdout/stderr、读取字节、token、messages/schema与判断已重算，详见[机器摘要](reflection-provider-routing-2026-11-13.json)。

## 实施：只改变可信配置与传输，不增加模型权限

用户随后明确要求将复杂反思做成可配置provider。本轮复用既有ReflectionModel接口、build_messages/translate及Core最终跨度/版本/水位/身份验证。schema1本地profile兼容；schema2显式选local或OpenAI-compatible Chat Completions。选择在服务可信启动配置中完成，变更需重启，不由问题、难度规则、模型输出或失败状态自动切换。普通存/取/查、画像、人设、维护队列没有增加云端依赖。

- 线上需服务配置、客户端allowlist及每次allow_remote许可。MCP另需可信启动标志；模型工具参数不能自行打开外发。dsh以默认false的可信reflectionAllowRemote选项传递启动及调用许可，原工具仍只有query参数。未开启时省略新参数，兼容旧本地调用。
- key仅从可信provider host环境变量读取，集中部署时由独立服务持有；兼容standalone CLI/direct Python本身也可成为可信host。worker通过stdin接收，HTTP Authorization使用；不写配置明文、账本、命令行、日志、SDK响应或宿主提示。发送前按原值扫描payload，输出在解码后的结构化字符串中检查，包括内层JSON和含引号/反斜杠的key。
- 发送前再次回源普通原文，明确私密/受保护/当前不可用输入在向量和远端调用前阻断；普通私密、真实加密收件箱/保险库内容不进入packet。只发当前查询有界探索取得的授权摘录，没有全量档案、人设或生成反思再增信。
- 云端仅对本机按原问题及调用方显式视图选出的摘录作最终判断，不能自行扩大外发范围；返回search/read/navigation/view会在任何新探索或下一HTTP请求前终止。packet以allowed_actions能力元数据给出四个final动作，没有新增记忆语义提示。本地探索保留，共用证据、final契约与校验，不要求provider拥有相同探索权限。这是出机授权策略，不是将final-only反事实成绩当准入。shell/写入/权限/私密解锁等仍被拒；撤回/身份变化清除所有推断，已发送内容无法从供应商事后撤回。
- HTTPS为默认，测试用literal loopback HTTP需显式允许。无代理、重定向、HTTP重试、流式或跨provider回退。隔离stdlib worker取消/超时后kill并reap；响应字节、重复键、深层异常、usage、完成原因及引用均校验。HTTP错误不回显供应商正文。
- token、墙钟和成本独立受限；线上默认每轮4096/512，可显式调整且仍受总预算硬上限。本地上限和冻结实验不变。输入是保守字节预检，不冒充任意供应商精确分词。按配置价格预留每轮完整上限，失败不退款，实际返回usage成本另列。费用是本机预算，不是供应商账单保证，也不是月度限额；完整限制见[provider契约](../contracts/reflection-providers-v1.md)。

## 验证与产物

最终源码联合93项通过，其中2项确保受限诊断不能误获准入；取消/超时测试补充核对实际child已退出。strict mypy151个src/tests文件及2个诊断文件、TS typecheck/build和Node语法检查通过；浏览器插件生命周期/CSRF两项通过。测试均为合成数据。

独立只读审查发现并修复四类边界：MCP参数单独申请外发、嵌套JSON/opaque凭证扫描绕过、深JSON RecursionError未封闭，以及远端可用无关新搜索扩张下一轮出机证据。最后一项用ORCHID_CANARY跨session普通来源真实复现：旧归档回归RED产生4次请求，修复后GREEN仅1次且search_rounds仍1，新主题未进入请求。旧wheel保留在外部rejected-egress目录。真实HTTP worker回归覆盖每项，不以字面扫描的单一测试冒充全部解码层覆盖。

新建外部非editable环境安装归档Core0.1.22和冻结的既有开发依赖，最终48项反思/协议/service/provider测试通过，补充取消回收单项通过。实际Python SDK、服务API、CLI、stdio MCP、归档TypeScript SDK共用同一假API，SDK/CLI/MCP不持有key也可正确调用；无可信MCP启动标志时，即使模型传allow_remote=true也无HTTP请求。

归档Core与preview.25在真实dsh的一次显式深查中通过，假API接收1次请求；返回临时低信任结果、原文引用及费用预留，未给维护或外部动作授权。首个源码宿主测试的断言曾把JSON字符串中的转义字段当作裸对象字段；修正表示层断言后，安装版场景通过，原失败日志保留。这不是线上模型质量实验。

69个Core .py/.mjs成员逐字匹配源码/wheel/新安装，34个SDK及37个Web归档成员匹配构建目录/解包产物。Node产物复用既有固定依赖树的外部junction，没有声称新依赖解析或全新宿主安装验收。产物仅在checkout外准备，未发布Release或替换用户现有安装：

| 产物 | SHA256 |
|---|---|
| evernook-0.1.22-py3-none-any.whl | 4a3833cefaa2683e9954b7cf42a2a64f43575fe985f943547d048d8532168f5d |
| evernook-harnesses-0.1.1.tgz | 0cfe50a377fb39bdf1d9a4b597b526b1e5f30aa0e1a41b5e544c2518bc7fe0b4 |
| dsh-evernook-0.1.0-preview.25.tgz | 6269810cdfac547d17981dac8b70bde98fa41215f4aa6f3fa936f3802a11df3f |

目录`evernook-120-provider-release-v1`保留安装核对和实际dsh日志。原frozen corpus/scorer/policy与反思语义协议不改；没有新增本地模型、候选原八例、真实付费API或真实线上质量/费用/时延/规模/日用准入。服务崩溃、物理OS生命周期与新长跑不在本轮验收中。下一步须由用户在可信服务环境配置实际endpoint/model/key后，按真实profile独立跑冻结评测与资源/费用门禁；模拟回答不算模型能力证明。
