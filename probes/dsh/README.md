# R0 dsh 接入探针

对应 [T01 / Issue #2](https://github.com/destinyTaoTao/evernook/issues/2)。这是合成数据实验，使用真实 Cordis、dsh Agent Loop、JSONL 持久化与本地附件插件；模型提供方是确定性的本地测试适配器。没有 mock 宿主事件、会话、工具执行或持久化。

**它不是记忆服务，也不是已安装桌面版默认 profile 的接入插件。** 普通桌面、CLI 启动、部署和真实私人数据均未验收。生产接口、更正/遗忘、安全分流、资源预算及可靠流式采集留给对应票据。

## 运行

已测：Windows x64、Node **24.11.1**；依赖固定为 dsh **0.2.1-rc.3**、Cordis **4.0.5**。需要 Node 24 的原生 TypeScript 执行与 `node:sqlite`，后者目前会输出实验性功能提示。

```powershell
Set-Location E:\project\evernook\probes\dsh
npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm run probe -- --output E:\evernook-lab\my-new-dsh-run
```

输出目录必须是**不存在的绝对目录**，其父目录须已存在，且不能指向源码仓库（包括经 junction 指入）。安装依赖需要网络；测试适配器不调用云端或下载模型。实验没有挂载默认用户 profile、遥测、终端或云模型插件，也不读取已有聊天与配置；没有进行操作系统断网验收。

`npm test` 单独运行全部测试，默认把运行数据保存在系统临时目录；`npm run probe` 另存测试输出、依赖版本/许可/代码哈希和环境清单，便于核查。单文件可用 `node --test --test-timeout=30000 test/host.test.ts`。

## 链路与边界

1. 真实 `agent.followup` 将合成输入追加到宿主 inbox；此处没有耐崩溃回执。
2. 原生 `agent/request` waterfall 等待 `ctx.sessions.flush`，随后将该会话的来源头和事件写入实验 SQLite 收件库。成功事务后才记提交水位，失败阻止模型派发。
3. `systemPrompt.context` 将已采集文本以带来源的、不可信证据注入后续会话的运行时上下文。该实验直接回放少量合成输入，不提供生产检索或事实推断。
4. `agent/turn-stopping` 保存已结束步骤的证据；显式 `capture` 可提交最终 `turn/end`。`rescan` 通过真实持久化读句柄补齐事件，重复重扫不增加记录，同源不同内容拒绝。
5. 强杀子进程分别停在宿主 flush 后、收件库提交后和助手流中途；重启后核对已确认输入与缺失范围。进程强杀不等同于断电或磁盘故障测试。

收件库以 **会话 ID + 事件 seq** 幂等；它不实现宿主入口按**消息 ID**的去重。测试明确记录：同 ID 消息在回合结束后再次 `followup`，会触发第二次执行。流式协作取消保留 `interrupted` 前缀；强杀时尚未提交的流片段无法靠重扫找回。

文件附件仍由宿主本地附件库保存；收件库不复制文件字节，不把文件内容加入普通上下文。图片、音视频和其他宿主入口未验证。完整结论及验收矩阵见 [报告](../../docs/reports/r0-dsh-2026-10-29.md)。

## 依赖与公开数据

锁文件固定直接和传递依赖及 npm integrity。直接 dsh/Cordis 包声明 MIT；测试工具及图像附件依赖的各自许可保留在安装包和锁文件中。仓库没有复制上游实现，也不将 `node_modules` 纳入提交。

仅使用源码内的合成文本与合成附件；原始实验日志、SQLite、WAL、宿主会话、附件及失败记录保留在仓库外。此探针没有凭证脱敏或保险库能力，不能接收真实私人数据。
