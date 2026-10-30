# T15 实际本地语义链路验证

`verify.py` 使用已有固定本地 MiniLM 候选和真实 MemoryRuntime、CLI 子进程、stdio MCP；不替代模型、数据库或宿主协议。输入仅为脚本内合成记录与查询，输出必须是仓库外尚不存在的目录。

先准备 [R0 固定模型和依赖](../core-compare/README.md)，在开发虚拟环境安装 `.[dev]`，按[语义契约](../../docs/contracts/semantic-v1.md)创建外部 embedding 配置，再执行：

```powershell
python probes/semantic/verify.py --output E:\evernook-lab\semantic-new-run --embedding-config E:\evernook-lab\embedding-candidate.json
```

脚本验证两条中文改述、精确查询、来源片段、保护占位不入向量索引、CLI/MCP 真实结果、实际模型超时和缺模型降级。另记录一条无答案查询的实际候选数量，不用单条样例承诺开放世界零误召回。`results.json` 保留合成输入、来源、查询、候选、空间、覆盖及完整调用测量；数据目录与访问文件留在外部私有子目录。

不下载模型、不调用聊天生成、不变更用户现有宿主或系统网络配置。每次调用含本地模型冷载入，不是约 1 秒或正式质量/性能验收；进程级出网拒绝的边界见语义契约。
