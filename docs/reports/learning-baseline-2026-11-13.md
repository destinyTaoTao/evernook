# #121 学习闭环基线冻结

交付独立的[zh-learning-v1](../../eval/zh-learning-v1/README.md)、严格评分器和公开Core采集器。既有zh-memory-v1及#81策略未改。33个案例×4个arm×6个维度共792个评分单元；手写31条来源，确定性生成10001条干扰，展开结果另行hash绑定。套件SHA-256：`b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1`；policy：`6b39e961a91fb1311fa498b2db80a61f1cfa64bb62813f31c8ef9f07e901a4ba`。

## 真实基线

仓库外产物：`C:\Users\dengtaotao\Documents\Codex\2026-11-13\evernook-learning-121-final`；资源采样为同级`evernook-learning-121-final-monitor`。保存observations、逐项details、评分report、collection和进程树原始jsonl。完整重评分与原report逐字段相同。

配置：固定MiniLM multilingual L12 q8/384维、revision `2c4055b12046f11709e9df2c122e59ffbdc2f900`；空间artifact digest `371b2499827ee7eac1bea9acf238546b2255aa688a34159917c77442859b761d`。有限规则处理器`automatic-preference-rules-v1:cc4636ef65c3d6c3fae66a69745cb32854a0568c24f738c609b7d5fcfceee300`仅在新建合成运行库显式启用；无chat模型或日常用户配置变更。实现身份从实际import包目录计算；本次为源码，完整身份留在collection。

| raw维度 | passed | failed | missing |
|---|---:|---:|---:|
| 候选召回 | 19 | 7 | 7 |
| 引文/版本 | 18 | 8 | 7 |
| 时间/冲突/未知判断 | 0 | 0 | 33 |
| 自动画像 | 24 | 2 | 7 |
| 沉淀与再准入 | 0 | 0 | 33 |
| 实际宿主回答 | 0 | 0 | 33 |

另外三个arm各198单元均missing。总计61 passed /17 failed /714 missing，`outcome=failed`、`admission=pending`。候选Recall@10为**已观察26/33例上的0.75**；不是完整套件得分。4次已观察的自动接纳中1次错误，另有1例漏提取；不能用均值掩盖。失败集中于跨年同义、多跳、冲突双方、单次例外和长段落偏好/后续反例。长输入仍实际报错并记missing。大位置场景超过默认64条合成库预算，真实删除、恢复和换版协议也明确missing。

27次实际召回调用（含长度拒绝）的p50约903.63ms、p95约944.33ms；只有小夹具召回调用，不含初始化/画像处理/宿主生成，也不是完整轮次、深查或规模准入。监控运行59.83秒、239次样本、4次进程读取失败；观察到进程树private bytes峰值582991872。采样后root退出；一次残留PID候选已证实是实验结束后新启动的系统backgroundTaskHost复用PID。采样中观察到且仍属于实验时段的存活PID为0；未观察到的短命后代/孤儿及推理中取消仍无证明。公开pre-canceled检查有原始错误回执，不能代替推理中取消。

## 验证与复核

执行并通过：

```powershell
$env:PYTHONPATH='src;tests/contracts'
.venv/Scripts/python.exe -B -m unittest test_learning_evaluation test_evaluation
# 30 tests passed
$env:MYPYPATH='src;.'
.venv/Scripts/python.exe -B -m mypy --strict --explicit-package-bases src/evernook/learning_evaluation.py tests/contracts/test_learning_evaluation.py probes/admission/learning.py
# 3 source files passed
.venv/Scripts/python.exe -B -m evernook.learning_evaluation validate
```

独立fresh-context AI review查出五项重要缺口，已处理：撤销/物理删除分离、跨固定前缀的历史位置场景、明确连续居住的来源、宿主实际文本与自身引文、实际import安装身份。另补Recall覆盖率以避免只交一个好例的误读。针对问题新增负向测试，观察到失败后修复；最终正控制覆盖完整矩阵，也始终不颁发准入。人工金标准审核未执行。

首次mypy把直接脚本和namespace import识别为两个module名；采用显式package bases及`MYPYPATH=src;.`后通过，未跳过检查。未跑无关全量测试、宿主回答或安装验收。

## 接续

#121完成的是冻结评测基础和当前失败基线。后续#112先解决固定前缀和长输入意图，沿用本套件对照；#113依据长段落偏好和反例失败扩展提取与保守发布。#118/#119/#120补齐各自arm，真实恢复/删除、规模、chat模型和实际宿主仍按对应ticket及#81/#84验收，不在本票宣称通过。
