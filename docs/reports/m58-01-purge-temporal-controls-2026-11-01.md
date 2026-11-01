# M58-01 / #59 删除后的时态控制

## 结果

在公开 `PurgeWorkflow.preview/purge` → 重启 → `FactRegistry.query` 链路复现：删除新住所来源后，旧住所重新成为当前事实。首个回归在修改前失败。

删除事实决定时，清除正文、来源片段、候选/处理器、确认和原请求；保留无正文时间、修订、替代及解决关系。查询继续先选择时间链，再检查可见性。当前来源不可用返回 `source_unavailable`，旧时段保持有效上界，修订及已删除请求身份不会回退或复用。

## 验证

- `python -m unittest discover -s tests/contracts -p test_facts.py -v`：13/13 通过。
- `python -m unittest discover -s tests/contracts -p test_purge.py -v`：15/15 通过。
- `python -m mypy src/evernook/facts.py src/evernook/purge.py tests/contracts/test_facts.py tests/contracts/test_purge.py`：4 文件通过。
- 新增回归涵盖现实变化、纠错、其他主体、历史上界、清理中断、重启、重试、CAS 和请求身份复用。
- 双轴代码审查：Standards 0 项，Spec 0 项。审查另以合成公开入口补验删除过去时段纠错、根节点及计划/争议解决决定；未发现复活，额外组合未全部固化为测试。

## 边界

验证使用本机合成数据，不代表日常宿主、生产质量或性能验收。完整 Python 套件另行记录。已有归档/备份和宿主副本残留语义保持不变；旧快照控制重放属于 #65，不由本票解除恢复限制。
