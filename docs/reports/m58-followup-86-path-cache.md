# #86 — 路径加载时未使用正文的额外模型读取

2026-11-08，接续[arena对照](m58-followup-86-arena-comparison.md)。定位并消除一个明确的额外JS文件流读取；模型资源/质量准入仍未通过。新策略只在独立实验profile显式选择，缺省profile保持`library-default`。未修改第三方包、原图/候选图、精度、默认日常profile或冻结评分。

## 触发条件与证据

固定Transformers.js4.2.0的`FileResponse`构造器创建`fs.createReadStream`，用`data`监听器向Web ReadableStream入队。Node路径加载分支取得文件名后不消费正文，该读取仍继续。8MiB合成文件的真实库路径解析回归在调用方未读正文时读完8388608字节，旧路径的零读取断言失败。

真实last-hidden图上的只读流跟踪进一步记录：初始哈希919096911字节、`FileResponse`额外919096911字节、每轮推理前的哈希919096911字节。这些是JS文件流计数，不包含ORT自身的原生文件加载，也不等于物理磁盘I/O次数。跟踪器只观察`end/bytesRead`，没有添加会主动开启流动的数据监听器。

官方[环境配置](https://huggingface.co/docs/transformers.js/api/env)提供全局自定义缓存接口；当前固定包的`CacheInterface/getModelFile`接受本地路径字符串。通过精确的本地图路径键及其库生成的远端缓存键返回同一个已校验文件名，可以跳过构造该图的`FileResponse`，仍由ORT正常加载文件。

Registry发布包SHA-512与原锁文件一致。发布包内`FileResponse.js`、`hub.js`、`cache.js`及实际使用的Node bundle与安装文件逐字节一致，分别记录于外部`framework-provenance.json`。关键SHA-256：

- FileResponse源码：`3552856187db8e11f687aafc69ddc570c1edb3283abfcd1addeb8ed1ebc7e073`。
- 实际Node bundle：`4932ec78a6b136d97d09a12093afb476530d9aa099dbaf1f9822ad56bfe2bc3d`。

归因限定于该固定发布包；没有根据当前文档页面推定其他版本具有同一实现。

## 最小实现与保护

`LocalQwenChat`实验profile可设置`file_access: "verified-local-path-v1"`。仅对固定`onnx/model_q4.onnx`的两个精确键返回本地路径；其他键未命中，写缓存拒绝。原有九项固定资产完整哈希仍在加载前和每轮生成前执行，不以文件名、mtime或缓存命中代替验证。模型图仍为`e9370b411c56b37bfeb48f2787655c62b014eb17d8fb441d3fc281ef0d01e3ce`，没有导出新图。

库缓存配置处于共享env。`withModelFileAccess`串行协调本包装器的pipeline构造，包括缺省构造；成功或失败都恢复原开关和对象，避免跨模型误用路径。startup记录实际策略和命中数；资源/原生驱动另保存新辅助模块的源码哈希。非法显式策略（含null）在读资产前拒绝；未配置仍沿用原路径。未提供运行中的缓存切换或任意文件路径解析服务。

五项回归通过：精确键/拒绝写入/旧状态恢复、失败后恢复与缺省构造等待、非法路径与队列恢复、两个并发模型互不解析对方路径、非法profile值早拒绝。真实复制模型加载后追加配置换行，即使JSON含义相同，下轮也在原生forward前拒绝（forward0）；错误模型身份同样拒绝。源模型资产保持原样。

## 实际结果

同一捕获请求，1321输入token、32768窗口、4096记忆预算、512输出上限不变。下表使用关闭arena的独立实验进程，未把它改为默认分配配置。

| 实验 | 结果 |
|---|---|
| 原路径对照（新代码缺省profile） | 保留一次额外FileResponse读取；prefill输出与原参考完全一致，峰2688278528字节，13.551秒 |
| 显式路径策略prefill | 只保留两次必要哈希流、额外读取0；全部输入feeds/56KV与最后logits一致，命中2次；峰2386214912字节，13.550秒 |
| 同实例两轮完整生成 | 各1321输入、自然119输出token；全部IDs/文本与原参考一致；额外读取0、三次必要哈希流仍执行；峰3095154688字节，55.100秒 |

这些是单次有界诊断，不是p95或硬峰值保证；OS缓存未控制，部分验证与类型检查共享机器。上述主prefill/生成采样54/54/220次、query failure0，操作上限分别35/100秒。原型cache preload的独立记录也保留，没有用其较低数字覆盖最终集成结果。

额外JS读取已被消除，但原生/整体内存仍高于2147483648字节。不能把少读919MB说成峰值减少919MB，不能晋升模型或关闭#86/#81。

## 真实SDK与恢复检查

使用当前Core源码、真实dsh SDK0.2.1-rc.3和独立合成g01-q6会话；该版本沿用冻结诊断环境，不是新的rc.5 Web宿主或完整工具工作流验收。两个新夹具均只存原题允许的一条来源，黄金答案不传给模型。原生输入分别986/980token，因独立来源UUID及上下文不同，不是上面1321token的字节相同对照。

- 正常流24.314秒、60秒上限，采样97次/query failure0、峰3010899968字节；首段独立读取和最终回放一致，原生可见137字符。原评分器的host-g01-q6仍failed，回答未满足逐字引用；其余正式题保持缺失，不产生新质量通过结论。
- 取消12.797秒、60秒上限，采样51次/query failure3、峰2916917248字节；在2token停止，记录AbortError/aborted，流为canceled、complete=false、final_message_id=null。质量观察明确标为`diagnostic_canceled`，不计成功答案。
- 宿主清理后，以新MCP进程读回并再次重开，正常全文和取消前缀均逐字一致；6.541秒、30秒上限、26采样/query failure0。相关诊断进程最终0，外网尝试均0。TS typecheck/build、差异检查通过，未重跑全量Core/SDK套件或224题。

## 复验与剩余范围

`model-file-access-check.ts PROFILE NEW_EXTERNAL original|verified`使用固定库和8MiB合成文件，不运行模型。original保留非零失败回执；verified为0额外reader/0正文读取、命中2次并通过。每次使用新目录，原始输出不能覆盖。该小回归与实际模型/SDK记录共同覆盖真实调用路径。

在复制出的独立实验profile设置上述`file_access`字段，再按既有`prefill-resources.ts`或`model-resources.ts`入口运行；核对startup策略与命中数，保留35/100秒总操作界限。全部外部证据位于`Documents/Codex/2026-11-08/evernook-86-file-response-01`，含发布包对照、源文件快照、失败/通过的小回归、流计数、原生会话、严格评分、篡改拒绝及新进程回放。模型/数据库/请求/输出/凭证未提交。

后续仍需定位原生权重/工作区及连续生成保留，或另做独立ORT格式等价性验证。完整质量、变化输入、2GiB达标、pi、真实远端模型及#81/#82正式日用门禁保持开放。
