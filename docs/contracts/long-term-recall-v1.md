# 长期候选与查询计划契约

Core0.1.16 / dsh preview.23；接口版本保持v1，新增可选参数及输出。

`recall`的`query`可以是完整原始输入（最多1048576码点），另可传`query_intent`（非空、最多256码点）。完整输入和意图都先检查凭证及UTF-8编码；无法使用意图洗掉原始输入中的受保护内容。`context`/HostContext接受同一可选意图。MCP的Recall/BuildContext及服务/CLI均传递该参数。

没有显式意图时，256码点内原样查询；长输入先去掉尾部空白以定位最后实质字符，再选最多256码点的尾部，优先保留完整末句。原文不变，query_plan含kind/query/original_chars/represented_start/represented_end/limited，原文坐标以Unicode码点计。显式意图的坐标为null，表示独立请求而非逐字原文。bounded_tail总是报告query_coverage_limited。下游应显示“按部分输入检索”，不能表示整段输入已完整理解。

Recall另可传`since`（含）/`until`（不含）作为调用者明确选择的发生时间范围。条件同时作用于字面与语义候选，先过滤再取候选；不自动从模型猜测年份。无条件时保留未知/未来时间的全局机会；近期标签仍遵守既有参考时间规则。

语义先过滤权限、普通原始证据、当前内容/控制版本及时间范围，在同一个 SQLite 读取快照内对每个合格片段计算一次精确 cosine。每个来源保留最佳片段。全局和近期分别按距离、来源 ID 取至多 100 个候选，再各自参与 RRF。近期窗口使用与全局时间条件的交集。最终 limit 仍为 1..100，普通 context 独立受 token 预算约束。

`semantic_scanned_chunks` 记录成功的单次精确检索实际计算距离的合格片段数。同一片段进入两个通道时只计一次。每个来源可以包含多个片段。`semantic_scan_limited=false` 表示没有应用固定片段扫描上限，通道仍使用各自的候选上限。取消、超时和索引落后需结合 `degraded` 和语义状态判断。

全局字面、全局精确与近期字面使用同一个 SQLite 读取快照，复用该快照核验的词法覆盖和待处理来源。每个通道仍独立匹配、过滤时间并截取候选。词法覆盖及其临时表只在这次读取事务内复用，后续请求重新核验。最终结果继续按当前可见性和来源版本回源校验。

semantic_search_backend区分disabled、python-exact及sqlite-vec-0.1.9-exact。可选C扩展不改变索引schema；Python路径也完整搜索，超过预算时明确降级，不再永远隐藏固定后缀。异常向量不能被报告成完整的无匹配；字面证据可在语义失败时继续提供。发布前继续按权威账本回源/检查来源控制，受保护证据不进入普通通道。

词法索引 ready 时，关键词通道从查询提取至多 64 个中文相邻双字与英文词，按 OR 匹配 FTS。候选按每个来源的最佳 BM25 分数及来源 ID 排序，核验普通原文资格、可见性、内容与规范化版本、时间范围及当前输入排除后，取最多 100 个独立来源。原文跨度逐字核验，RRF 每通道每来源只计一票。

同一个读取快照中，先按完整排序键挑出最多 256 条 FTS 行，再逐来源核验资格并去重。FTS 命中已取尽或当前批次已确认 100 个独立来源时，这批候选确定了最终排序。其他情况执行完整的资格过滤与排序查询。批次打分遇到 FTS 虚表损坏且读事务仍有效时，也可尝试完整查询。两条路径使用同一时限及取消事件。索引处于其他状态时，精确、普通词法与语义通道继续按各自的覆盖和降级状态返回。

服务配置同时包含embedding_config与maintenance_access时，独立维护线程在前台让行策略允许后处理小批量语义索引，状态位于status.service.maintenance.semantic。每tick默认最多1片段/5秒；暂停、前台和退出会停止推理及提交。普通embedding失败单独报告，不饿死现有scheduler。模型空间变化后的pending/ready按该空间独立计算。

Core 0.1.60 起独立服务将嵌入配置和后端绑定到进程生命周期；前台语义召回、上下文、主题和反思，以及后台语义／主题维护复核当前配置，合法内容变化或启动时无效后修复都返回 `embedding_config_changed` 并要求重启服务。格式错误继续报原有错误，普通采集及独立调度不因此停止；没有反思配置的服务即使启用了后台维护，也可在嵌入配置无效时启动，语义及主题维护报告不可用。管理诊断对合法但尚未重新绑定的配置显示 `restart_required`。等价 JSON 排版变化不算变更；恢复成启动时同一配置可继续使用。已经开始的调用可能使用原绑定完成，变更不能撤回已交付内容；不提供原地热切换，重启后按新空间独立构建，原文保留。该约束不绑定独立的上下文／tokenizer 配置，也不声称自动发现或修复历史混用的向量。0.1.61 起，已激活账本重启时，即使显式配置反思，嵌入不可用也只禁用反思；首次激活仍拒绝，修复配置后须重启。

#193 先以隔离服务复现合法配置变化未提示；最终源码定向 10 项、非 editable wheel 定向 8 项、反思调用前阻断测试在源码及安装版各 1 项、源码及安装版页面组件各 2 项和 strict mypy 通过，覆盖重启、无效后修复、后台隔离、并发配置读取和真实 MCP 采集。反思测试使用替身，不代表真实模型质量。此前服务／维护／provider 套件 30 项在最终快照读取细化前通过；未进行真实模型质量、规模性能或浏览器验收，日用门禁不变。

取消从dsh AbortSignal经MemoryClient/MCP传到服务；直连stdio的recall/context也各有协作取消事件。SQLite progress handler及提交前显式检查维持错误语义。取消不是撤销已经完成的原始证据写入；刷新结果仍以实际状态及回执为准。

本契约是功能及覆盖语义，日常质量/时延/资源准入沿用#81/#84；#121冻结套件的失败、缺失及实际宿主回答继续分别报告。

## 可选来源候选索引

直接维护模式下，有 `maintain` 权限的本地操作者可在写入维护窗口启用索引。以下示例中，`$runtimeConfig` 和 `$clientFile` 分别是既有运行库配置和调用端凭据文件的路径。

```powershell
evernook prepare-recall-candidate-index --config $runtimeConfig --access $clientFile
evernook status --config $runtimeConfig --access $clientFile
evernook rebuild-recall-candidate-index --config $runtimeConfig --access $clientFile
evernook remove-recall-candidate-index --config $runtimeConfig --access $clientFile
```

新库默认关闭。`status` 的 `recall_candidate_index` 表示两个索引的定义均已核实。Python 入口为 `SemanticRecall(runtime).configure_candidate_index(access, enabled=True)`，传入 `rebuild=True` 可重建，传入 `enabled=False` 可移除。已激活服务模式的库遵循现有维护入口限制。

SQLite 为有非空普通原文的消息维护两个部分索引，按来源 ID 提供内容版本与时间，按发生时间定位近期窗口。来源抑制、更正、生成内容排除和向量版本仍在检索事务内核对。向量通道继续计算全部合格片段的精确距离，最终证据返回原文。构建和重建需要额外磁盘，在一个写事务内完成。配置失败会回滚。索引缺失或定义不匹配时，查询使用原有来源路径。遇到同名异构对象，维护命令返回 `recall_candidate_index_conflict` 供操作者核对。
