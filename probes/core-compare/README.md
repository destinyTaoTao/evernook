# T02：内核与离线候选比较

这是 [Issue #3](https://github.com/destinyTaoTao/evernook/issues/3) 的可复现实验，比较未改动的 MemOS `2.0.16-beta.1` 公开 MemoryCore 与最小 SQLite 原型。实验结果见[报告](../../docs/reports/r0-core-2026-10-29.md)。本目录没有生产服务，不能输入私人数据或真实凭证。

## 在线准备

已测 Node `24.11.1`、Windows x64、Python `3.14`。运行数据、模型与结果目录必须在源码仓库之外。

```powershell
Set-Location E:\project\evernook\probes\core-compare
npm ci --ignore-scripts --no-audit --no-fund
# 仅安装 better-sqlite3 已锁定版本的原生预编译件；不运行 MemOS postinstall。
Push-Location node_modules\better-sqlite3
node ..\prebuild-install\bin.js --runtime=node --target=24.11.1
Pop-Location
python prepare_models.py --output E:\evernook-lab\r0-core-models
```

模型脚本固定 embedding / 抽取模型的仓库 revision，下载所需 ONNX 与 tokenizer 文件，核对上游提供的 LFS SHA-256，保存每个文件的哈希及下载/校验耗时。候选权重约 118MB + 919MB，另有 tokenizer；下载和安装不属于离线运行。断线保留 `.part`，下次重新下载该文件，成功文件先校验再复用。

MemOS npm 包声明 MIT；其上游总仓库顶层为 Apache-2.0，不能将两者混称为同一许可。模型转换仓库未声明明确许可元数据，基础 MiniLM/Qwen 模型声明 Apache-2.0：仅作为本地验证候选，分发转换权重前须补齐许可链。`node_modules` 和模型不提交仓库。

## 禁止进程外网访问的运行

```powershell
npm run typecheck
npm test
npm run compare -- E:\evernook-lab\r0-comparison E:\evernook-lab\r0-core-models
# 上一条输出目录中的 run-... 是本轮原始证据目录。
python bridge_probe.py --run E:\evernook-lab\r0-comparison\run-REPLACE
python summarize.py --run E:\evernook-lab\r0-comparison\run-REPLACE
```

比较目录和模型目录均须已存在。每次比较新建 `run-...`，不覆盖旧轮次。Node 执行前拒绝 fetch/socket，Transformers 禁止远程模型，关闭 MemOS telemetry/hub/host fallback；失败不静默切换云端。该进程级拒绝不等同于关闭系统网卡，也不证明原生库的所有潜在网络行为；系统断网验收仍待补齐。

使用同一固定集合：94 条合成消息、24 条预标注查询，其中 80 条近期噪声；首轮 API 结果与三轮暖模型测量分开。MemOS 关闭反思、抽取、重排等模型链后比较检索，两条路径使用相同 MiniLM embedding；另外直接调用 Qwen 并经 MemOS 公开 LLM facade 验证离线抽取候选。没有把独立抽取样例当作 MemOS 完整沉淀链路。

`measurements.json` 与 `retrieval-raw.json` 保留查询、来源、候选、上下文、耗时；`lexical-path.json` 只计词法候选，不能代表完整请求；`writes.json` / 导出 / 重建 / 依赖失败 / 修订探针分别保留原始结果。`manifest.json` 固定输入、锁文件、源文件哈希、模型与机器配置。Python→Node 常驻 stdio 的 60 次实际索引请求单独测量桥接成本，不冒充 Hermes 接入验收。`summarize.py` 从原始结果重建分位数和命中率。

探针限制：最小路径缺少生产更正/抑制、时态事实、无答案决策和并发恢复契约；MemOS 的更新/删除语义也不是本项目的原文保全契约。测试通过表示这些有限行为可复现，不表示产品不变量全部满足。最终模型与语言选择由 T04 确认。
