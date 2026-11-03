# 三类偏好候选探针

显式指定一种模式，输出目录必须是仓库外尚不存在的固定 NTFS 路径：

```powershell
.venv\Scripts\python.exe probes/preferences/verify.py --synthetic-selector --output '<新的仓库外目录>'
.venv\Scripts\python.exe probes/preferences/verify.py --extraction-config '<仓库外本地 Qwen 配置>' --output '<另一个新目录>'
```

synthetic-selector 只测确定性选中全部来源后，分类策略及候选提交是否符合 16 个独立预标注样例，不能作为模型成绩。另一模式复用固定本地 Qwen，配置与资产要求见[抽取契约](../../docs/contracts/extraction-v1.md)；没有下载器或云端替代。

16 个场景各自领取独立任务，模型模式每次重载模型，避免把不同情景合成同一用户上下文或一次生成挤满上限。report.json 记录每项预期/实际分类、任务状态、处理身份、机器与 Python、各次 process 调用的墙钟及总和、来源/时间上限；初始化与原文录入耗时不包括在内。模型内存、p95 和正式质量/硬件准入保持未测。任一任务失败时，包含预期空结果的检查也判失败，不能把模型崩溃当作安全过滤通过。原始记录留在输出的合成运行库中，重跑必须换新目录。
