# 已抑制、撤回和删除来源的本地反思复测

同一冻结集中的七道题，在控制记录过滤前后各运行一次真实本地 Qwen3-0.6B。普通反思的检索和证据均从 3/7 提升到 7/7。严格判断从 3/7 提升到 4/7。抑制后、撤回后、归档恢复后及彻底删除后的四个问题，在过滤后都收到空证据包，模型均弃答。

| 冻结场景 | 过滤前：动作 / 证据条数 | 过滤后：动作 / 证据条数 | 严格判断，前 → 后 |
| --- | --- | --- | --- |
| control-initial | 回答 / 1 | 回答 / 1 | 失败 → 失败 |
| control-continued | 弃答 / 1 | 弃答 / 0 | 通过 → 通过 |
| revoked-initial | 回答 / 1 | 回答 / 1 | 失败 → 失败 |
| revoked-continued | 弃答 / 1 | 弃答 / 0 | 通过 → 通过 |
| control-restored | 回答 / 1 | 弃答 / 0 | 失败 → 失败 |
| purge-initial | 回答 / 1 | 回答 / 1 | 通过 → 通过 |
| purge-continued | 回答 / 1 | 弃答 / 0 | 失败 → 通过 |

基线和候选分别来自独立 Git 导出的 `8a67d3e` 和 `d929d1b`。两侧使用相同模型配置和本地资产，在新的合成账本运行公开反思接口，执行真实的来源控制、归档恢复和确认删除。使用已建好的词法索引，未配置语义模型。记录范围是这七个与变更直接相关的问题，宿主最终回答、持续运行的发布决策与其余 26 例未在本轮收集。完整判断门槛仍按[既有全量归因](2026-11-21-reflection-failure-attribution.md)和[更大本地模型诊断](2026-11-15-local-quality.md)独立验收。

在各自导出目录，使用相同模型配置、冻结语料与 Python 3.14 执行以下命令。结果目录须为新的仓库外目录。每个结果目录都有逐题 `details.json`、冻结评分 `report.json` 和实际 worker 输入、输出。原始两臂保存在此次运行的临时目录 `evernook-ctrl-reflect-bb02a6e714584fe48574341dd2114a82`。

```powershell
python -B -m probes.admission.reflect --output $resultDir --model-config $localConfig --cases control-initial,control-continued,control-restored,revoked-initial,revoked-continued,purge-initial,purge-continued --full-lifecycle --max-fixture-sources 64
```
