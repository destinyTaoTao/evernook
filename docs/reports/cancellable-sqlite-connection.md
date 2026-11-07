# 可取消检索连接的SQLite配置等待

2026-11-08，实施[#104](https://github.com/destinyTaoTao/evernook/issues/104)，关联#101/#94。

Core0.1.2/preview.19的真实dsh短pilot在后台词法索引/导航并发时返回`evernook:storage_busy`。保留失败记录，未自动重跑工具或把失败计为成功。对独立安装版MCP工作线程做临时诊断，仅记录SQLite异常身份和函数/行号，确认失败在`search → _connection → PRAGMA synchronous=FULL`，并非工具结果写事务。诊断已从环境移除。

可取消检索使用SQLite零等待连接，靠期限/取消检查和VM进度回调结束操作；连接配置同样会遇到短暂WAL锁。Core0.1.3仅在交出连接、开始业务查询之前，对该配置语句的精确`SQLITE_BUSY`以不超过10ms的片段等待。总等待受原`busy_timeout_ms`限制，每次尝试均检查原检索期限/取消回调。配置成功后的SQL、事务、采集与工具执行均没有新增重试；普通连接继续沿用原SQLite等待。持续锁、其他SQLite错误和取消/超时仍显式失败，连接始终关闭。

## 实测

- 真实SQLite排他锁最小红例：原实现短锁读回报`storage_busy`，取消/期限测试收到错误种类也不符；2.59秒内复现。修改后新增5项通过，覆盖短锁解除、取消/期限、原配置等待上限，以及业务SQL错误/业务争锁均只执行一次。
- 既有检索18项、启动2项、MCP20项、Core及新增测试strict mypy通过。未运行全量业务套件。
- 独立非editable安装的Core0.1.3包内45个Python/Node文件与wheel、源码逐字节一致，安装版新增5项也通过。wheel SHA-256：`2b7b62f0972deaf4b67cfa8b77609306d2f9370c1112c3298707b3473dd60f85`。
- 安装版Core0.1.3、preview.19、固定dsh0.2.1-rc.5第六次短pilot通过：6个前台混合流/推理read回合，取消、权限拒绝、宿主退出/重启、客户端重连及只读目标新写请求拒绝均完成。原副作用内容/mtime不变，重复确认无额外模型/工具执行，外网尝试0。后台最终词法369/369、导航游标369，queued/running/failed均0。
- pilot观察约69.97秒，整体94.90秒；379次250ms采样，进程树PrivateBytes最大785391616字节。28次进程查询失败计入原始报告，短命子进程/退出边界可能遗漏，不声称硬峰值保证。此pilot不替代1800秒正式窗口。

验收入口为`tests/contracts/test_connection_setup.py`及#101实际安装探针。旧失败pilot和冻结policy均保留；后续正式目标仅更新产物身份到Core0.1.3/preview.19，不修改窗口、前台间隔、采样或资源门槛。真实模型、物理系统与日用准入仍未通过。
