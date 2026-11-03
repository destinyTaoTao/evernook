# M58-17 离线三类偏好草案

公开接口为 `evernook.preferences.PreferenceProcessor(runtime, backend).process(access, source_ids, timeout=120, cancel=None)`，backend 复用既有 `ExtractorBackend` / 固定本地 `LocalQwen`。可信本地 CLI：

```text
<python> -m evernook.cli extract-preferences --config <runtime.json> --access <access.json> --extraction-config <profile.json>
{"source_ids":["<saved-message-id>"],"timeout":120}
```

配置、输入上限、模型文件哈希、断网限制、协作取消和失败状态完全沿用[离线抽取](extraction-v1.md)。不向普通 MCP 暴露此维护入口，不注册后台触发，不下载资产或切换云端。`ok=true` 只表示取得回执，须检查 run.state / error_code。

## 候选及策略

`CandidateWorkflow.candidates` / `candidate-list` 在既有草案上返回可选 `preference_category`：`response_style`、`technical_tool` 或 `general_interest`。来源片段保留 code point 坐标、state/content version、representation，run 保留处理身份；模型摘录不能改写为自由生成事实。

首版采用保守的**完整陈述识别**，不是开放域语义分类：只处理用户本人角色、模型标记 user_statement、无争议且只有一个完整来源的逐字选择。选中的子串不能丢弃整条原文中的引用、否定、条件或高风险上下文。quoted / roleplay / assistant_generated / disputed 不生成偏好草案；来源变化仍由提交事务阻断。

完整陈述还须符合代码内有限形式：例如“我偏好简短回答”“我希望回答先给结论再分点”“我常用 Python 3.14”“我喜欢观鸟”。回答形式、技术工具名与兴趣项目均为有限集合，详见 preferences.py 的识别表达式。未识别的新工具、兴趣、复杂多句或前后文一律不生成此类草案，可继续通过通用 extract / 原文路径审查。此取舍降低误分类，明显限制召回，不能声称覆盖所有中文偏好。

类别只是审查标签。所有结果仍为 candidate，automatic_acceptance=false、quality_admission=pending，不修改事实、画像、人设或任何权限；即使模型输出合法且标签匹配也不代表已通过自动更新准入。争议/未知内容不会被当作确定长期偏好注入。

## 身份、兼容与资源

偏好处理身份绑定 preferences.py 字节 SHA-256 和原 backend.processor_version 的 SHA-256，与通用抽取任务分开；改动策略不能复用旧成功结果。相同来源版本与同一策略重试复用既有任务；来源失效后候选读取为空。原有无类别草案的 payload、哈希与候选 ID 保持原样，新字段无需重写旧行。

PreferenceReport 继承原任务回执，另提供整个 process 调用的 elapsed_seconds 与 quality_admission。耗时包括租约/数据库操作，缓存重试也单独计时；它不是模型推理时间、硬性墙钟 SLA 或 p95。来源最多16条/16384 code points、推理预算最多120秒；实际模型内存和目标机质量/性能保持未测。

复现与分项资源记录见[探针](../../probes/preferences/README.md)。确定性 selector 只验证策略与持久化，不能冒充真实模型；缺失本地模型时只有失败回执和保留原文，不能把空输出当作安全过滤通过。
