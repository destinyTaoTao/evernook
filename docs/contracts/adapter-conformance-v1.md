# 适配器一致性契约 v1（adapter-conformance-v1）

状态：契约冻结（2026-10-31，票据 #54 / T40）。本契约约束任何新外壳（harness/桌宠/助理）接入 EverNook 的采集与注入行为；合规测试套件（`evernook.adapters.conformance`）按本契约产出通过/缺口报告。契约只描述外部可见行为，不约束适配器内部实现。

## 1. 配对与作用域要求

- 每个外壳经可信本地 `pair_client` 获得独立调用端身份；适配器不得复用其他外壳的 access 文件。
- 外壳默认使用 `HARNESS_DEFAULT_SCOPES = (recall, capture)`；提升作用域必须由可信本地端显式决定。
- 适配器不得绕过核心检查：所有读写都经配对身份进入 runtime/工作流，未配对或越权必须以核心回执原样失败（`unauthorized` / `permission_denied`）。

## 2. 采集语义

- **整次缓冲后发布**：适配器在宿主回合完成（completion=complete）后将该次输出的完整文本/帧缓冲提交 `submit` / `submit_capture`；不得在未完成时声称已保存。流式逐 token 显示不在契约内。
- **原文保真**：用户/助手文本必须原文提交（含标点、数字、emoji）；提交后按 ID 读回必须与原文逐字一致（敏感分流除外）。
- **幂等**：同一源事件（相同来源组合）重复提交返回原回执；内容不同返回 `source_conflict`，适配器不得静默吞掉。
- **敏感分流**：适配器可以声明事件 protected/sensitive，但不得自行"脱敏后仍声称保存原文"；核心的投影与 `original_saved=false` 回执原样呈现给宿主。

## 3. 注入语义

- 注入必须走预算上下文构造（ContextBuilder 或等效实现）：payload 是 `untrusted_memory_context` 信封内的证据，不得把记忆文本拼接成指令或赋予其权威性。
- 宿主提供真实 tokenizer 计数；不得用字符上限冒充 tokens。used_tokens 必须等于对 payload 的真实计数，且不超过预算。
- 超预算必须显式 omitted（带原因），不得静默截断后仍声称完整。
- 人设块与画像摘要按 #47 语义注入；已抑制/已更正内容不得出现在注入结果。

## 4. 采集缺口披露

- 适配器必须以结构化方式披露未覆盖的宿主事件：每条缺口为 `(event_kind, reason)`，`covered ∩ gaps = ∅`，且缺口非空（没有任何外壳能覆盖一切宿主事件）。
- Open-LLM-VTuber 参考适配器至少披露：语音/ASR 转写原文与置信度、视觉/多模态帧、Live2D 交互事件、工具执行流；文本轮次属于 covered。

## 5. 合规套件

- `evernook.adapters.conformance.ConformanceSuite` 对实现 `AdapterSurface` 的适配器运行固定检查（原文回读、幂等、敏感投影、预算诚实性、抑制排除、作用域纪律、缺口披露），返回 `ConformanceReport`（逐项 passed/detail 与总体结论）。
- 新外壳接入验收标准：全套件通过（本仓库内以参考适配器的契约测试为准）。
