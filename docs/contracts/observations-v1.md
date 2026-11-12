# 综合观察 v1

Core0.1.18/#118。观察是独立、版本化的派生理解，始终inference=true；原始事件、事实/画像、经历候选和观察互不替代。综合观察不改变核心人设、执行规则或宿主工具权限。设计借鉴见[Hindsight一手研究](../reports/hindsight-design-review-2026-11-12.md)。

## 证据与增量决定

ObservationDraft记录topic_key、conclusion、support、counterevidence、category、valid_from/until、processor_version、policy_version和disputed。每个跨度必须来自当前普通完整original事件，验证ID、角色、原始坐标、状态/内容版本及精确引文；支持与反例分别保留。投影提供来源元组、发生及获知时间。观察/摘要ID不能成为原始证据。重复事件/重复跨度去重；助手和工具材料保留角色但不计入独立用户证据，不用频次或置信度升级真值。

可信维护propose形成candidate，支持create/merge/narrow/change/correction；merge/narrow分别保留旧支持与反例，不能把反例改标支持。change与correction是不同操作，记录适用时间及历史修订，不修改原文；当前query只提供当前可用头，不代替FactRegistry的双时间历史查询。自由归纳不会自动发布。decide要求maintain/accept和实际用户确认来源，publish后无反例为published，有反例或disputed为conflicted；withdraw为真实不可用头。split同事务撤回父观察并创建待审子观察，支持/反例必须按各自立场完整保留。

每次决定有稳定request_id、请求指纹及expected_revision。重试不重复创建修订；相同request_id的不同负载拒绝。发布与来源复核同事务，提交前检查截止/取消。读时再次验证所有引用和发布确认来源；任一来源更正、抑制、删除或版本不符即阻断整条正文，不以剩余支持“投票恢复”已被反例质疑的结论。原始记录和过去派生版本仍按独立删除契约保留或删除。

## 后台、预算与去重

ObservationProcessing默认关闭；独立服务已配置维护身份并显式enabled后，在前台/暂停/关闭让行边界内处理。第一版自动构建仅覆盖现有有限偏好解析器识别的三类，形成review-only综合摘录；不声称已实现或准入通用模型综合。已有画像仍由独立逐类准入的PreferenceUpdates发布，观察工作身份不借此获得额外事实/人设权限。

原始输入→持久候选租约→规则摘录→观察candidate、来源进度和运行回执同事务提交；模型/策略换版形成新的处理身份。进程死亡后按租约到期重试；异常、取消和超时不留下半个已发布观察。删除聚合来源时，关联候选运行、观察回执及进度一起失效，幸存来源可重建，其他槽的已有回执幂等复用。

单批最多16个来源、总32个候选；服务每tick最多1来源/1秒，超出候选总量会收窄本批来源前缀，其余仍待处理。每个观察最多64跨度、合计16384字符、正文2048字符。一个槽超过64条有效依据时进入needs_review隔离，正文不参与query，原始atoms全部保留并可分页审查；其他槽继续推进，不能因预算丢反例或无限重试。溢出拆分会核验全部当前atoms的支持/反例，最多8个子观察/512跨度；超出明确拒绝，不自动截断。原槽不会被worker擅自恢复或重解释成单一肯定。

related是维护查找：完整结构化槽始终先于其他词面重合项，无固定历史前缀，分页最多32。它不运行问答SemanticRecall/RRF，不把跨通道共识变成去重权重；v1只承诺结构化槽和字面相关性，不宣称自由语义近义归并。status分别报告历史运行数、存储头状态、atoms与隔离主题数，不能用completed_runs表示发布成功。

## 服务接口、删除与恢复

所有CLI读取JSON stdin并使用--config/--access；在服务模式下统一经服务，不允许旁路直写。

| 命令 | 核心输入 |
|---|---|
| observation-configure/status/process | enabled；空对象；limit/timeout |
| observation-propose | request_id、draft、expected_revision、operation、observation_id |
| observation-read/decide | observation_id；request_id、expected_revision、action、confirmation_id |
| observation-split | request_id、observation_id、expected_revision、drafts、confirmation_id |
| observation-related/query | topic_key、query（仅related）、limit/offset |
| observation-evidence | topic_key、limit/offset；包括隔离槽的当前原始依据 |
| observation-rebind | request_id、draft、source_remap：旧原始ID→新原始ID |

普通recall身份仅可query已发布且当前有效的观察，不可读维护候选、配置或发布。可信操作默认10秒，允许显式timeout≤120秒，支持服务取消。query结果仍为低信任背景，当前尚未加入普通每轮ContextBuilder；#119通过有界主题视图接入。检索结果不能被当作用户的新授权。

普通删除清理受影响观察全部版本、派生引文、候选/任务及相关回执，原始事件按既有确认删除流程处理。控制检查点包含观察状态；恢复发布比较完整观察与维护状态，旧快照缺少较新撤回时拒绝发布。v1不会把观察的较新决定单独补抄到旧备份：需要新快照或显式重新生成，旧备份不能回活观察。原机全失恢复另由#117验收。

rebind要求完整且一对一的原始ID映射，逐项对照新机真实原文坐标、当前版本和权限，记录source_remap与原稿指纹，处理身份和策略改为rebind-review-v1；只生成新candidate，不继承旧发布、准入或客户端。无匹配依据时拒绝，不能仅因旧ID消失继续使用旧正文。
