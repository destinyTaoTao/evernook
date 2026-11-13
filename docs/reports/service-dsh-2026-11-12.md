# #110 独立服务的 dsh 存取查与管理闭环

Core0.1.14、Web preview.22。仅新建仓库外合成账本/profile，未切换日常profile。

SDK可配置serviceRequired，Web强制要求服务；stdio和可信管理桥接均接同一服务。普通身份启动时须capture+recall，独立维护身份须recall+maintain+accept。独立评审发现CLI重复检查发现文件会在移除竞态后直写，已改为required模式无条件请求服务，确定性RED→GREEN回归通过。

## 验证

- Python test_service/test_service_bridges/test_cli共22项通过。strict mypy覆盖cli、mcp_server及桥接测试3文件通过。
- Node源代码四场景通过，包括Web跨站/CSRF/关闭边界、真实dsh目标tokenizer模式保存/新会话召回/显式搜索/关闭适配器后独立客户端读取、Web页面更正与抑制对新会话实际provider请求的影响、缺capture启动拒绝。
- npm typecheck、插件build、前端语法check通过。
- 实际wheel安装到独立venv，实际tgz安装到新dsh profile；固定补丁宿主rc.5通过24项原发布/恢复回归。以该宿主解析所有native peers，三个服务场景再次通过，约69.3秒。没有使用真实远程模型。
- 源码/wheel/实装49个Core成员逐字节一致，源码/tgz/实装37个插件成员一致。宿主与profile共61依赖边核对通过，生产审计0条已知告警。

wheel SHA256为e21f51629238111df31018cb6885a29c11bbadccd2ab991d0f2385ae88e2e297，tgz为1511515035339f48b9d74debdfc128f58523bda0930b37ff0839a171bbef74ba。

证据、独立环境、包和日志保存在仓库外Documents/Codex/2026-11-12/evernook-service-106。首次直接加载tgz模块缺少由宿主提供的peer，第二次测试loader对CommonJS使用nextResolve(fileURL)失败；改用显式shortCircuit的本机peer测试loader后通过，原失败日志保留。第一次Web断言误把系统审计命中算作用户原话，按实际角色筛选后通过；随后请求使用了错误的operation字段形态，按既有页面契约修正测试后通过。

## 复跑

设置EVERNOOK_TEST_PYTHON为独立安装解释器、EVERNOOK_TEST_OUTPUT为新的合成证据目录。源代码场景运行：

```powershell
node --test --test-name-pattern="independent service carries|web service|web preview rejects" --test-timeout=120000 plugins/harnesses/test/dsh.test.ts plugins/harnesses/test/dsh-web.test.ts
```

安装包场景另设置EVERNOOK_DSH_TEST_HOST为固定宿主目录、EVERNOOK_TEST_WEB_MODULE为实际profile内lib/dsh-web.js绝对路径，清除指向源码的PYTHONPATH后运行：

```powershell
node --import ./probes/dsh/service-host-loader.mjs --test --test-name-pattern="independent service carries|web service" --test-timeout=120000 plugins/harnesses/test/dsh.test.ts
```

Web继续披露字符兼容模式，#105等待原生完整请求计数。#111仍需阻止旧直写、全入口一次切换、切换中断和新写后的恢复策略；本批不宣称完成日常准入、浏览器视觉复验、物理系统验收或正式质量/规模长跑。
