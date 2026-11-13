# #112 全历史候选与长输入闭环

Core0.1.16、dsh preview.23。实现[长期候选契约](../contracts/long-term-recall-v1.md)及[ADR0015](../adr/0015-exact-history-candidates-and-independent-query-intent.md)：现有向量表全历史精确候选、可选本机C距离、独立查询意图、ready FTS关键词候选、每来源去重/每通道一票、服务空闲向量化与取消。原始数据及向量格式不迁移。没有启用日常profile、模型质量准入、外部服务或云端fallback。

## 全历史位置的真实对照

全部沿用#121套件`b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`，固定MiniLM384维/原revision/0.4阈值。10001条独立生成干扰，加3条目标，目标输入位置为1/5002/10004。每条经公开submit实际保存、逐条回执留存；公开refresh实际生成10004向量，pending=0/failed=0，未复制向量伪装不同文本。

| 相同库/模型/原始问题 | 开头目标 | 中间目标 | 末尾目标 |
|---|---|---|---|
| 隔离旧固定前缀源码 | 命中 | 未命中 | 未命中 |
| 新全历史exact，词法未建 | 命中 | 未命中 | 命中 |
| ready FTS + 新关键词 + exact | 命中 | 命中 | 命中 |
| 最终独立安装版 | 命中 | 命中 | 命中 |

中间题“学会水上划行”对“学会独木舟”的相似度为0.355。它在前后两条纯语义路径都失败，失败页与独立阈值诊断分别保留。随后复用已有FTS提供“学会”等关键词候选，不降低语义阈值；最终中位仅由keyword通道取回，未伪报语义命中。末位原先没有扫描机会，新exact路径已能取回。

同一ready库的通道对照：完整通道3/3；移除keyword为2/3；移除recent_semantic仍3/3，但保留近期词法/窗口计算，不冒充移除全部时间通道。这是三个位置问题的候选对照，不是广泛质量收益证明。实体/事件图、模型重排未实测收益；继续保留现有RRF，等待#118/#119的可追溯资产再评估新增索引。

向量准备853.43秒，采样监控853.79秒/3411样本，2次进程读取失败，观察到树private bytes峰值603717632。词法追平另用33.33秒。最终安装版三次召回约2.14–2.21秒；样本少且与相关测试共享机器，不计算正式p95，不满足完整轮次≤1秒的证明。10万/100万、并发写入/重建、热候选≤150ms、近期词法≤50ms继续按#81/#84，未替代或放宽。

## 冻结小套件与实际宿主

保留三个独立配置的观察，不把缺失或策略变化混成增益：

| 策略 | 候选通过/失败/缺失 | 已观察候选Recall@10 |
|---|---|---|
| #121原MiniLM基线，词法未建 | 19/7/7 | 0.75，26/33 observed |
| 新exact/长输入，词法未建 | 20/7/6 | 0.7593，27/33 observed |
| 最终安装版，词法ready/关键词 | 21/6/6 | 0.7963，27/33 observed |

最终完整矩阵64 passed /16 failed /712 missing，admission=pending。引文19/8/6；画像24/2/7，仍有长段落偏好漏提取及反例后错误旧标签。时间/冲突/未知判断、实际模型回答、沉淀及其余三个arm继续missing。关键词通道还会取回有关控制的确认记录；这不是被抑制原文复活，也不等于问题的答案。候选回源正确、证据相关性和最终判断仍分别计分。

最终小套件召回调用27次p50约998.32ms、p95约1058.06ms，不含准备/画像/宿主生成；监控67.85秒/271样本，无采样失败，观察到树private bytes峰值585977856。所有这些数据都不颁发日常准入。

实际安装Core0.1.16与preview.23、固定rc.5打补丁宿主完成4个测试：长输入尾问题在字符和目标tokenizer两种模式进入真实provider请求；服务存/取/查及宿主关闭；Web控制后的新会话来源更正/抑制；缺capture身份拒绝。使用实际安装native/Web模块，清空PYTHONPATH。另独立安装服务在无宿主时真实MiniLM向量化1来源并通过独立意图召回。最终回答是合成adapter输出，不把provider拿到来源冒充真实模型正确引用/作答。

## 检查与修复

- 最终相关契约167项通过：semantic/search/FTS ID兼容/query plan/context/service maintenance/bridges/service MCP/purge。未重跑全部仓库测试。
- strict mypy：src/tests122文件通过；两个新probe另行通过；TypeScript typecheck及构建通过。
- 初次独立review的直连MCP取消、尾部空白、refresh提交前取消及语义错误饿死scheduler均修复。可复现缺陷有定向回归；语义错误不阻断独立沉淀，权限失效及前台/暂停/退出仍停止。
- 新关键词组件独立review发现现有合法FTS重复行可能挤占来源/重复增分；110条重复行负向测试先失败，修复为候选截断前去重、RRF每通道每来源一票，相关旧兼容回归通过。
- 监控后查询观察到的PID：位置准备有5个晚于实验结束创建的PID复用，非后续复用的存活候选为0；最终小套件存活候选为0。只覆盖采样中观察到的PID，不证明未观察的短命后代或孤儿不存在。

## 可复算产物

根目录`C:\Users\dengtaotao\Documents\Codex\2026-11-13`：

- `evernook-112-position-fixture`：原始回执、aliases、逐批真实索引水位；`evernook-112-position-monitor`：资源原始样本及postcheck。
- `evernook-112-position-query`：旧prefix、新exact原始结果及中位阈值诊断；`evernook-112-position-keyword`：关键词结果与通道对照。
- `evernook-112-final-position`：最终安装版三位置结果；`evernook-112-final-quality`及同名`-monitor`：完整冻结评分与原始页/回执/资源。
- `evernook-112-final-installed-semantic`、`evernook-112-final-installed-dsh`：无宿主真实维护与安装宿主合成运行库；`evernook-112-final-artifact-receipt.json`：逐文件核对。

最终wheel SHA-256 `8dbb35a7dfe883c925cac8c902aa78689bcc8cc5c945eb35b4b1f0c78f0ebbfb`，53个Core代码/worker文件与安装及本机工作树字节一致；npm SHA-256 `eb3f9b8a8792684a8e82d0e0211fec80489241219d04b45ded426609e933454b`，37文件与独立安装包一致。工作树部分历史文件为CRLF，Git按属性规范化文本；异机重建不保证相同包hash，应重新核对实际产物及处理器身份。前一轮未含keyword的候选wheel/环境保留，不能混称最终版本。

准备probe启动后仅补了`ids: dict[str,str]`类型标注；原始manifest不改，probe-reconciliation记录原hash/现hash，去掉该标注可逐字节重构原准备probe。向量模型/切片身份不变；后续查询单独记录当前安装实现与原producer，未重绑旧运行身份。

复算入口：`probes/admission/history_positions.py prepare/query`、`probes/admission/learning.py --lexical-ready`、`probes/installed/semantic_service.py`。均要求新建checkout外输出，明确预算和缺失；模型/配置路径由可信本地参数提供。未发布包到registry，也未替换用户安装。
