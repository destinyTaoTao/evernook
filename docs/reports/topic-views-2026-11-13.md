# #119 主题视图实施与验证

Core0.1.19实现可选用户专题、可重建快照、分开的证据/控制/观察水位、合并刷新任务及普通上下文接入。定义/编辑/暂停/删除由可信入口控制；普通recall身份仅读取当前可用快照。实现契约见[topic-views-v1](../contracts/topic-views-v1.md)。默认未配置、没有自由模型摘要，也不修改核心人设。

## 机制及已修复问题

原始保存与单行水位同事务；背景按view_id合并最新目标，一次有界刷新一个视图，持久token/租约保障崩溃续跑。旧正文在新证据或控制变化后即不可读，不能等后台刷新才撤下。完整观察支持/反例一起纳入预算或一起省略。全库保守失效会增加无关更新成本，不声称已实现精细依赖增量刷新。

独立复核确认并修复三个边界：不同操作复用request_id的指纹碰撞；只hash直接文件漏掉传递解析器变更；显式用户编辑被原专题历史scope过滤。均补回归；编辑锚另存原始user证据，明确标注scope_exception，仍遵守原始权限/敏感/版本检查。系统提示、partial stream和projection不算新原始证据，避免每轮metadata使快照永久失效。

实际服务发现嵌套dataclass结果在提交后无法序列化，topic edit和既有observation split表现为service_disconnected。新增受限递归JSON序列化器，同时补实际transport的编辑重放及两个split子结果回归，RED→GREEN。

分词期撤回回归另发现无视图的普通上下文也可能留下旧原文；统一复用分词前/后来源和派生head校验，变化时清空完整记忆payload，避免重复原文残留。显式known_at排除当前视图，不能把当前缓存冒充历史知识。两项RED→GREEN，追加独立指定增量复核4项通过，无确认遗留重要问题；这不是全仓审计。

## 实际验证

- 最终独立安装Core0.1.19：主题26项及实际transport/后台权限2项，共28项通过（51.460秒）。包括实际worker在持久claim后os._exit(71)，租约过期后attempt2只提交一个快照；原始写入/编辑失败回滚、控制并发、TTL、暂停删除、purge、旧定义恢复拒绝、新归档去除/重装已知hook及未知SQL拒绝。
- 实际固定打补丁dsh0.2.1-rc.5和已安装preview.23，独立Core服务：原专题进入provider；来源抑制后下一轮排除旧正文；显式编辑后新专题带新原始引用。1场景通过（45.417秒）。此前源码SDK为rc.3，其1场景35.459秒不冒称rc.5。未改用户profile或全局安装。
- 最终源码主题/上下文/服务/维护/恢复/归档/删除联合115项通过（253.278秒），strict mypy134文件通过。期间出现一条unclosed SQLite connection ResourceWarning，未定位归属，不能声称资源无泄漏。TypeScript typecheck与probe strict mypy通过。安装、冻结采集和源码组部分并行，时延不是隔离性能准入。
- 初次上下文组46项中45通过、1项因新增最终budget检查把既有recall_timeout降级抛出；修正为超时清空证据、保留原降级契约后重跑最终组。此前42项组中的Windows文件锁清理错误，以及独立复核36项组中的维护重启超时均保留；各自定向复跑通过，根因未确认，不能声称修复。

## 冻结评测与边界

实际安装probes/admission/views.py使用同一套件b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1与原policy。按原始问题定义有界专题，不读取gold选择来源；真实固定MiniLM索引+ready FTS。只收集实际configure/refresh/read的retrieval/evidence，无模型推断或宿主回答伪造。

27/33观察，views retrieval21通过/6失败/6缺失，evidence19/8/6，Recall@10=.796296；全矩阵40/14/738，admission=pending。与#112 raw同分项相同，未证明学习收益。恢复/处理器/规模/purge协议未采集者保留missing。小夹具定义+刷新+读取p95约1416.440ms，包括构建，不是普通缓存读取或完整聊天时延；资源、物理OS与规模未测，#81/#84门禁保留。

## 产物

仓库外C:/Users/dengtaotao/Documents/Codex/2026-11-13：evernook-119-wheel、evernook-119-installed、evernook-119-installed-dsh、evernook-119-final-quality、evernook-119-artifact-receipt.json。源代码/安装的早期失败根保留。wheel SHA256=a068989dd5cb49071ed0d273cdf5c371826d9639672c6948cb25b67373838ec5，61个Core代码/worker成员与本机工作树/wheel/安装逐字节一致；异机Git LF重建应重新核对实际处理器身份。npm生产代码未变，复用已核对preview.23与固定61依赖边的rc.5宿主。

旧备份缺较新定义/删除控制时拒绝运行库发布；不是自动把新决定抄入旧备份。导入快照不被当作可信可用正文；来源ID/跨度仍须当前可验证。自动通用ID映射、自由模型综合、#120反思及日用质量不由本票声称完成。
