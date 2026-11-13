# #113：完整段落画像与跨会话候选

Core0.1.17，继续分支codex/dsh-memory-preview。本票实现有限规则的完整段落低风险偏好处理、可信服务管理与审查用经历候选；不扩大身份/关系/敏感事实、人设或执行权限，不启用日用profile。

## 实现与规则边界

整段上下文识别与精确子句跨度共享同一解析器，抽取与最终发布均复核。长期偏好、本轮例外、历史、计划、否定纠错、引用与冲突分开。明确否定纠错复用dispute/profile draft，暂停旧标签但原始消息保持active。相反的肯定陈述继续要求可信用户澄清；支持correction与change两种既有时态动作。处理器身份绑定新解析器字节，旧准入不能沿用。process.processing报告抽取、发布、澄清、忽略及来源角色/时间/去重身份，未执行观察层明确not_run。

跨会话候选复用持久候选租约和原子沉淀队列。默认不在后台生成；显式启用后仅在当前批次符合范围时生成review-only摘录，保留每来源完整跨度、角色、发生/获知时间和原始事件。字面实体提及不等于实体身份或关系推断。来源更正/抑制使整条正文不可用，可指定当前来源重建。规范摘录读时重算，防止通用候选用合法ID为伪摘要背书。见[契约](../contracts/episodes-v1.md)。

独立复核确认并修复四个问题，均先复现RED再GREEN：逗号前临时限定丢失；含“其实”的否定漏识别导致早期肯定误发布；否定其他兼容风格误撤当前值；历史长段落草案漏做冲突检查。复核者独立重跑四项通过。其他整段高风险/引述排除保留；旧复合兴趣排除改为独立类别处理，理由见实施计划Ruling。

## 验证

- 源码偏好/解析/经历/旧抽取/澄清/画像拒绝/调度/沉淀/服务联合104项，首次103通过、1项服务重启退出。该退出未保存stderr，未确定根因或归属；补启动失败诊断后同项连续3次通过。后续独立服务组结果另列，不将重跑等同根因已修复。
- 最终服务完整6项独立复跑通过，启动失败现在会保留stderr诊断；未对生产启动行为增加重试或掩盖退出。
- 安装版原文摘录/解析/服务19项通过，包含可信服务画像处理、跨会话候选与普通身份写入拒绝。安装结果来自独立wheel环境，未用PYTHONPATH指向源码。
- 源码及安装Core0.1.17各运行一次真实dsh SDK/provider测试通过。临时详细回答保留简短长期标签；新长期值触发澄清并暂停旧标签；可信correction后下一轮profile及facts采用详细回答，旧值不再确定性注入。检查的是实际provider请求，合成adapter回复不代表真实模型回答质量。
- TypeScript typecheck、源码strict mypy（126文件）及评测探针strict mypy通过。独立评审只覆盖本票diff及四项修复，不等于全仓安全审计。

## 冻结评测

使用原#121套件b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1和原#81 policy，未改金标准或阈值。实际安装MiniLM/ready词法策略：raw automatic为27通过/0失败/6缺失，4次accept中误接纳0；长段落、长期/本轮、反证、引用与冲突均按已观测案例评分。真实待澄清来源通过公开fact/profile读取采集，不再把未采集澄清映射成missing。此前#112相同策略为24通过/2失败/7缺失。

全矩阵仍为67通过/14失败/711缺失；检索21/6/6、引文19/8/6，观察/视图/反思、模型判断/宿主回答和恢复/处理器换版阶段仍有missing，不能称完整准入通过。Recall@10=.796296，27/33 observed；小夹具调用p95约1046ms，非规模或完整聊天轮次测量。外部监控265采样、查询失败0、观察进程树Private Bytes峰576413696，根进程正常退出；采样可能漏短命或孤立后代，不承诺硬峰值。

## 重现与产物

外部合成根为C:/Users/dengtaotao/Documents/Codex/2026-11-13：evernook-113-final-quality（观察/回执/分项）；evernook-113-final-quality-monitor（资源/命令）；evernook-113-installed-dsh（实际provider运行库）；evernook-113-installed（独立安装）。原始失败保留在首次联合命令输出，未更名为通过。

关键命令：PYTHONPATH=src;tests/contracts下python -B -m unittest test_preference_updates test_preference_statements test_episodes test_preferences test_preference_clarifications test_profile_rejections test_scheduler test_consolidation test_service；独立安装19项使用PYTHONPATH=tests/contracts。dsh运行node --test --test-timeout=120000 --test-name-pattern='independent service paragraph profile corrections' test/dsh.test.ts，安装运行清空源码PYTHONPATH。learning.py原命令和模型配置路径见外部monitor/manifest.json。

wheel SHA256=3da952c0a67ee7a422611e6cca174b2f14baf9c1ca18179583051cd9586086b4；55个Core代码/worker成员与当前工作树、wheel及独立安装字节一致。沿用#112关于工作树CRLF与Git LF可能影响字节/处理器身份的限制，其他checkout须重新绑定准入。无npm发布、用户运行库迁移或日用profile启用；#81/#84质量/规模门禁及本次未定位重启退出继续披露。
