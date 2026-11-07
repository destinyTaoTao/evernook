# DUI-1 — 可信页面管理核心

Ticket: [#89](https://github.com/destinyTaoTao/evernook/issues/89), part of [#88](https://github.com/destinyTaoTao/evernook/issues/88).

新增可信本地 `MemoryManagement` 与固定 CLI `manage`，为后续页面提供分页画像/草案/来源/历史/时间线、版本绑定预览和确认。同一事务保存真实页面操作的确认记录、原话更正或画像决定及精简回执；请求重放不重复执行。来源抑制与移出画像保持不同含义。复用原有来源控制实现，仅将既有 change 事务体提取为私有方法供该事务组合。

只把 recall/maintain/accept 身份作为可信调用端；普通采集和模型无页面确认接口。预览只持久化操作HMAC及ID/版本，不复制正文。页面/宿主仍须建立真实用户点击、连接鉴权和跨站保护，详见[契约](../contracts/management-ui-v1.md)。

验证：新增管理契约 5 项通过，覆盖真实CLI、权限/调用端隔离、内容绑定、过期、并发版本变化、来源派生失效、跨实例确认回放、SQLite故障整体回滚、Unicode分页和原文保留。既有来源控制8项通过；源码及新增测试严格 mypy（42文件）通过。

本增量未实现浏览器页面、dsh安装、跨处理器拒绝防重、独立事实争议裁决或正式日用准入。测试只用合成数据；没有全量Python/SDK重测或真实模型性能声明。
