# 中文学习闭环冻结基线 v1（#121）

这是合成语料和可复算的分项评分协议，不是质量准入证明。33个案例、31条手写来源及10001条确定性生成的干扰来源；原始文件、展开结果及既有[#81策略](../../probes/admission/policy.json)分别绑定SHA-256。没有私有对话、真实凭证或权重。

四个arm固定为`raw`（原始证据）、`observations`（加综合观察）、`views`（再加主题认知视图）、`reflect`（显式深查）。每个arm都必须提交完整案例矩阵；未提交的行或维度记missing，不推断通过。阶段为initial/replay/continued/processor_changed/restored。后续功能必须沿用本套件；若金标准有缺陷，另发版本并保留原结果，不能为实现调低答案要求。

## 内容与边界

覆盖跨年、无明显词面重合、未知年份、长输入、多会话偏好和单次例外、否定/引用/反例、现实变化和纠错、多跳、证据不足、冲突澄清、来源抑制/撤销/真正删除、旧备份、注入、助手推测、重复重投与处理器换版。全历史位置场景按输入顺序在第1/5002/10004条放置目标，穿过原10000片段固定前缀边界。它仍不是#81的10万/100万规模门槛。

来源中的话都是不可信材料。只有corpus步骤中经过固定schema验证的controls可驱动公开控制API。`revoke`是逻辑撤销；`purge`须运行真正的确认删除协议，不能映射为撤销。`restored`须真实恢复旧备份后执行审查；`processor_changed`须实际换版检查准入，不能把普通查询改名为这些阶段。

## 分项评分

- retrieval：前10个去重来源的候选Recall；另计required全部命中且forbidden不出现的完整案例通过。无证据案例要求空结果。报告observed/expected/coverage，缺失不得用高Recall掩盖。
- evidence：逐一检查当前来源版本、原文坐标、原文quote以及语义锚点。正确ID配错误片段不通过。额外无关引用不通过。
- judgment：固定decision/value/source_ids，分清回答、弃答、澄清和拒绝；按category单列时间、冲突和未知案例。
- automatic：固定受控标签及来源；区分错误接纳和未提取，单列incorrect_accepts/observed_accepts。没有接纳样本时错误率为null。临时指令不能改长期标签，反例应撤下错误标签。
- maintenance：目标是该题的结论或指定的长期偏好；independent_support为支持该目标的独立原始事件数，重投/派生重述不增加。助手/工具推测不构成用户偏好支持。readmitted表示处理器此阶段是否具备发布资格；换版未准入必须false。本维度只能来自真实处理/发布观察，不能用输入数量计算冒充。
- host_answer：记录实际完整answer_text及其自身citations，再独立检查引用与版本；不能用候选命中代替宿主采用。此v1采用受控答案字符串，真实宿主应请求固定结构、完整保存原始响应并严格解析；人工语言质量评审仍未执行。

observations格式见测试正控制。配置绑定模型身份、处理器、预算、实现及policy；配置不同的报告不能直接相减。阶段换版必须另存处理器身份、原始回执和准入结果。既有#81质量、时延、资源与宿主门槛不变。即使正控制全通过，评分器也保持`admission=pending`。

## 执行

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -B -m evernook.learning_evaluation validate
.venv/Scripts/python.exe -B probes/admission/learning.py --output E:\lab\NEW-learning-run
.venv/Scripts/python.exe -B probes/admission/learning.py --embedding-config E:\models\embedding-profile.json --output E:\lab\NEW-semantic-run
.venv/Scripts/python.exe -B -m evernook.learning_evaluation score --observations E:\lab\NEW-semantic-run\observations.json
```

评分CLI返回0=完整通过，1=有失败/无效输入，2=只有缺失；采集CLI返回0只表示采集完成。每次必须使用新建checkout外目录。使用既有`probes/admission/monitor.py`包裹采集获得进程树资源样本，保存其report及原始jsonl；样本峰值不等于硬上限。

当前collector仅观察公开Core原始召回和在隔离合成库显式启用的有限偏好规则。普通查询超长时保留实际错误；不读取oracle来缩写问题。默认最多64条来源，大历史位置案例、物理删除、恢复、处理器换版、综合观察/视图/反思及实际宿主均保留missing。pre-canceled检查不冒充推理中的取消；未测资源/残留保持null。每次查询单独保存回执、处理器hash、控制结果、覆盖水位和原始page/profile；时延仅统计召回调用，不能当作普通完整轮次或深查延迟。

独立AI冻结复核发现并修正了删除/撤销混淆、候选位置缺口、连续居住证据缺口、宿主独立引文和安装身份绑定问题。修复有负向回归；human gold review未执行。后续真实模型、完整矩阵、规模和安装宿主仍需独立准入。
