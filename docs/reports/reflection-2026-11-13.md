# #120 反思基础实施及未通过的模型验收

Core0.1.20/preview.24实现默认关闭的显式本地只读反思基础，接口/预算/来源失效和dsh接线已验证；固定Qwen用途质量未通过，#120保持开放。[契约与配置](../contracts/reflection-v1.md)。这不是日常反思能力已交付或项目全部完成。

## 已实现及取舍

服务持有一个ReflectionEngine，可信启动文件选择固定本地模型与normal/deep分离预算；普通身份只能查询。可信reflect-configure显式安装既有三水位，查询不建派生资产、不获取维护身份。内部只允许search/read/已发现会话邻近导航/指定view/done，无通用工具分派、云回退、外部执行、人设或保险库读取。完整原始问题在向量/模型前检查凭证信号，当前输入receipt排除。

真实worker复用固定Qwen文件hash、transformers4.2.0/ORT1.24.3；完整模板无截断计输入，直接generate保留实际输出token IDs（包括EOS）。总输入/输出、每轮4096/512、轮次、工具、候选、回源span/字符、墙钟及并发独立受限。worker无有效meter即usage.metering_incomplete，不以零计数伪装零成本。超时可返回最终仍验证有效的原始依据；过期到无法复核则不交付正文。综合结论永远unverified_model_inference，合法引用不证明语义支持，state complete仅表示协议合法结束。

缓存默认0，仅可选进程内，绑定身份、输入、当前input、模式、视图、策略/实际代码/模型及三水位；每次命中仍回源复核。结果不自动写事实/画像/人设或观察。用户若要沉淀仍需独立候选审查，无自动晋升捷径。

独立复核确认两处问题并RED→GREEN：MCP省略timeout时默认40s传输短于60s反思执行；最终撤回仍留下模型unknown正文。修复默认70s传输及清除全部模型文本，coverage只留query hash/长度，避免派生搜索词也泄露旧正文。当前input排除贯穿各读取及cache。独立定向3项通过。

复核另指出工具回流；105条反思tool_result的回归复现原始依据被挤掉。修复普通记忆消费在候选limit前排除结构化evernook_memory_reflect工具产物，覆盖字面、关键词、向量维护/召回、主题/观察来源及内部导航。raw read/search保留审计，用户原始日志不被删；助手/工具不独立增信。FTS未建/ready及观察span拒绝RED→GREEN，独立该项通过。不能识别任意助手改写或外部重新标记的副本；不宣称通用语义血缘解决。

## 实际功能验证

- 反思/服务/MCP/CLI/主题/上下文联合102项通过（196.108秒）；工具回流增量后反思/服务/semantic/search/观察/主题联合113项通过（117.520秒）。有交叠，不相加；strict mypy139文件、评测probe strict mypy、TS typecheck/build通过。
- 最终非editable安装41项通过（67.883秒），含实际认证transport→MCP stdio、普通身份只能查询/可信配置拒绝、敏感预分流、持久视图状态与缓存/来源控制。
- 实际子进程测试分别超时/取消回收；最终安装真实Qwen worker在spawn后2s取消，2770ms返回reflection_canceled，结论空、保留已验证原始引文，owned worker退出1且剩余0；token meter未完成明确披露。只证明该进程回收，不是ORT所有时序/物理OS或规模准入。
- 源码dsh SDK rc.3以buffered模式完成显式工具→下一provider含原始引用（25.002秒）。初次错误用了需要补丁的durable_tools，得到tool_publication_patch_required；清理已识别实验服务，测试helper补失败清理，未放宽生产门禁。
- 最终安装Core0.1.20/preview.24和固定补丁rc.5，实际durable_tools深查工具→原始审计保存→下一provider收到低信任结果/原始ID，1场景通过（32.875秒）。最终宿主61依赖边核对、npm生产审计0条；没有改用户profile。该场景只验证带证据返回，包括模型降级，不验证回答正确。

## 实际固定模型失败

最初无ready索引试跑没有证据而耗轮；补ready索引后仍有非法动作，均保留外部根。最终安装在#121原冻结suite/policy上独立实跑6个initial案例：多年回忆、未知年份、多会话关联、证据不足、冲突及注入。配置deep30s/3轮/8192输入/1024输出、真实MiniLM与ready FTS；不读gold选来源、不改阈值/评分器/语料。

reflect retrieval5通过/1失败/27missing，evidence5/1/27，judgment1/5/27；全矩阵11/7/774，R@10=.833333（仅6/33观察），admission=pending。只有unknown/multi/conflict三例产生合法done，但其结论复读提示模板“有证据的推断”，不是有效答案；其余分别因invalid_citation/invalid_action降级。判断投影明确区分失败回退弃答与完成模型推理，不能把唯一正确弃答归功于模型理解。没有运行最终宿主回答、自动更新、恢复/处理器变化或规模分项。

6例整体p95约23238.935ms，包括本地worker冷启动/推理/检索；不是普通召回、常驻warm性能或正式p95。峰值资源未测；此前Qwen资源与质量门禁不变。不能靠放宽JSON/引用校验、抄金标准或认定引用合法来关闭#120。

## 产物与剩余验收

仓库外C:/Users/dengtaotao/Documents/Codex/2026-11-13：evernook-120-final-wheel、-final-installed、-final-node、-final-quality、-final-real-cancel、-final-installed-dsh、-final-artifact-receipt.json及contracts日志。早期model-pilot/model-pilot-ready、source-dsh失败、provisional wheel/node均保留，不能混称最终产物。

最终wheel SHA256=06e7f5d0aa479f450c0b42bd679575f5790455140fbbde96acf60b4530a23461，65个Core代码/worker与本机工作树/wheel/安装相同；tgz SHA256=74a03121bcbf0583b6444fd9f3fa3d22238d60daaec4fb0ec9f6bc9007feb44a，37个成员与工作树构建产物/包/隔离安装相同。Git文本换行规范化会影响异机实际代码hash，重建需重新核对处理器身份。未上传registry或切换用户库。

剩余：受限模型的未知年份多跳真实答案、证据不足可靠弃答、冲突澄清及逐断言语义支持均须通过；当前0.6B模板复读不能当推理。#120最后一项及#81/#84用途质量/规模门禁保持开放。后续应先定位固定模型的模板复读/动作及引文格式问题，再比较经验证的本地模型候选；不扩大权限或自动启用。
