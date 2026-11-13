# #120：结构化解码、引用绑定与4B对照

基线52fd11f，沿用修复后的binary-file链路。本轮五个条件各完整十例，没有合格候选，未接入生产。4B的refs-schema条件正确描述了助手历史猜测，也改善了数量格式与弃答理由引用；但多跳引用退步、总体严格分仍6/10、owned工作集超过2GiB，不能据此宣称问题已解决。

原始问题、角色、冻结gold与评分函数不变。补充集SHA256仍为`eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0`；原zh-learning-v1及其policy/expectations不改。原33例和宿主回答不在本次provided-evidence诊断范围内。

## 方法与归因边界

预先记录于计划Task11，固定贪心seed12009，十例完整跑完，不逐题改参数或挑seed。筛选线维持严格>=8/10、可回答>=5/6、四负例错误肯定0、十例完整有效；单例30秒、4096输入、512全部输出、CPU4线程，owned private及working-set各<=2147483648bytes。失败仍保留，不改标签、不删除“项”、不归一化答案。

- **flat-schema**：消息和原引用协议完全保留，原生JSON-schema约束输出结构与可选证据ID；引用文字仍由模型复制并按原标准逐字检查。
- **refs**：只替换原support格式说明和原三个演示中的support字段；模型输出证据ID数组，程序只将已选ID绑定到当前完整原文。没有新增语义句/演示，也不更换问题/角色。decision、value和引用集合均不纠正；重复/不存在ID、额外字段及超出已有2048支持跨度上限的原文明确失败，不补来源或截短引文。
- **refs-schema**：在同一refs接口上加原生结构约束。schema不读取问题、角色、原文或gold；允许空support，保留search/read/navigation与已授权view动作。没有强制多跳引用全部输入、强制弃答或约束答案为gold。

refs投影后的原文准确性来自程序绑定，不能计作模型逐字复制或蕴含验证能力。原始wire和projected_text分别留存；原冻结judge只接收机械投影，decision/value/所选来源集合照原值严格评分。错误肯定即使投影失败也计数。结论仍为未验证模型推断，不产生事实写入。

新权重为[Unsloth Qwen3.5-4B UD-IQ3_XXS](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/blob/e87f176479d0855a907a41277aca2f8ee7a09523/Qwen3.5-4B-UD-IQ3_XXS.gguf)：固定revision、1949047968bytes、SHA256 `00aab66a2359a9afc52f0ab757e61f9be1656e6fcaba8468bd95579a8b2cbc40`，实际作者仓库API元数据与下载字节均核对。这是第三方量化，不称官方原件。权重、元数据及原始合成回执均在checkout外，不发布权重。

沿用固定llama.cpp b12360 CPU发行包和原生GGUF模板。结构约束依据[该版本GBNF/JSON-schema文档](https://github.com/ggml-org/llama.cpp/blob/2d8a32502909ca065c41c4cf5f2fb1c427c047fc/grammars/README.md)及实际CLI help核对。4B两个条件统一physical batch32、flash-attention on、K/V q4_0，logical batch128/context4608不缩减。真实日志确认生效：model1848.30MiB、KV40.50MiB、recurrent50.25MiB、compute32.16MiB。4B与2B比较同时含权重/执行差异，不能称单变量模型能力结论。

## 完整结果

通过/失败/无效/未观察均按冻结值，positive分母为六个可回答例。四负例观察到的错误肯定均0；无效输出仍失败，不能据此授予安全准入。

| 条件 | 通过/失败/无效/未观察 | positive | private峰值bytes | working-set峰值bytes | 采样/查询失败 |
|---|---|---|---:|---:|---:|
| 既有2B Q4原协议基线（52fd11f） | 6/4/0/0 | 4/6 | 570658816 | 1522810880 | 190/0 |
| 2B Q4 flat-schema | 6/4/0/0 | 4/6 | 570134528 | 1522343936 | 191/1 |
| 2B Q4 refs | 4/5/1/0 | 2/6 | 637280256 | 1522008064 | 168/0 |
| 2B Q4 refs-schema | 4/6/0/0 | 2/6 | 569311232 | 1522036736 | 168/4 |
| 4B IQ3 flat | 6/4/0/0 | 3/6 | 565866496 | 2207682560 | 518/0 |
| 4B IQ3 refs-schema | 6/4/0/0 | 3/6 | 566939648 | 2207703040 | 434/2 |

2B flat-schema与正确前缀基线的十条decision/value/来源集合一致：tool-data仍答“3项”，assistant-log仍空abstain，reported-guess仍缺理由来源。语法约束没有解决这组语义/格式错误。2B refs无约束多了额外evidence_id字段而被拒绝；refs-schema消除了该无效格式，但没有恢复语义分数。两个refs条件均把link-location答成柜名、link-object只引一个来源。减少复制任务未改善小模型总体表现。

4B原协议把tool-data答成严格gold“3”，reported-guess弃答且引用原话，修复了两处焦点错误；assistant-log改为有理由abstain，仍不应弃答。它又把link-location答成柜名、link-person只引接待人的名字，丢失连接证据。

4B refs-schema终于将assistant-log正确答为“陶艺”，并仅选择e1；assistant-guess仍refuse而非gold abstain。三个多跳案例全部失败：地点仍答柜名，三个都漏关系来源。相较4B原协议，一例历史描述改善和一例多跳退步抵消，总分和positive不变。不能把“能描述助手猜测”推广为已学会区分所有来源权限，也不能把refs视为引用完整性修复。

4B observed工作集约2105.4MiB，两个条件均超出2048MiB。即使低private、较少输出token或焦点例改善，也不能绕过mmap权重的工作集成本。50次均原生EOG完成；各条件最大输出40/20/20/39/17，最大单例墙钟约5.18/4.33/4.27/13.87/11.15秒，无超时。这些是合成短输入、进程冷启动的观察值，不是正式p95、独立规模或日用证据。250ms采样可能漏峰，文件缓存未控制；表中的查询失败全部保留。offline/local路径与环境清理是既有策略，网络attempt本轮未计量，不声称0。

## 验证与后续边界

五条件原始回执目录为`evernook-120-wire-<condition>-v2`，对应独立monitor目录；机器摘要保留report/resources摘要、资产/采集器身份、全部逐例wire与原严格判断。全部collector_unchanged=true。首个flat-schema-v1完成后，外部串行驱动因平台GBK读取UTF-8报告失败而中断；原回执保留，最终代码五条件统一在v2重跑，未改种子或择优记分。

本轮从原stdout/stderr逐例重算50条，核对messages、prompt/schema摘要、native实际读取字节、输入/输出token、原始wire/机械投影与严格评分；资源峰值从每条owned样本重算。测试覆盖不补漏引、不改答案/标签、非法/重复ID、畸形action、超长原文失败、敏感输入及无效引用不能隐藏错误肯定。原scorer、生产src和eval与52fd11f保持不变。

没有条件达到原筛选线，因此不开展候选原八例、择种子复验、生产native后端或新安装/dsh/取消接线。当前Core0.1.21/preview.24及默认关闭策略不变；#120和日用准入仍开放。下一步需要在完整关系证据和来源语义上同时达标的模型方案；本轮证据不支持继续把“缩短引用格式”当成通用质量修复。

验证：相关51项、strict mypy147个src/tests文件及9个probe/测试文件、Node语法检查通过。独立审查另跑相关10项，并从原字节重算全部50条、核对资源/摘要锚/当前采集器身份与IQ3作者元数据，未发现可行动问题；它未独立重复生成或重跑全部51项/mypy。最终diff检查通过、llama-completion残留进程0。未替换用户安装，未扩大云端、权限、维护写入或自动沉淀范围。
