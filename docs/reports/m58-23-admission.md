# M58-23 — Chinese quality and scale admission

Ticket: [#81](https://github.com/destinyTaoTao/evernook/issues/81), part of Spec #58.

Status: native quality campaigns completed; scale campaign stopped with partial evidence;
**admission blocked**. Missing scale observations and failed gates keep the ticket open.
No daily profile is activated.

The frozen policy is [probes/admission/policy.json](../../probes/admission/policy.json),
SHA-256 `6b39e961a91fb1311fa498b2db80a61f1cfa64bb62813f31c8ef9f07e901a4ba`.
The source baseline is `33d03d45c5c02fd1c10c6b26bd9d2fece949b28b`.
The 224-query suite is bound to hash
`b487e1777bd06eef30a50c3fc6811ef54e801c8bc00b3d46e00c6388b8d9886b`.
Its review is maintainer-delegated AI-assisted review; human review remains pending.

## Implementation

The production change adds `SemanticStatus.stored_vectors`: the actual row count in the
configured space, including stale rows. Zero means configured/not built; unavailable or
disabled backends report null. It is separate from indexed-message/current-coverage counts.
The public regression uses a literal three-fragment example and verifies partial/restarted
index construction. Existing retrieval, authority, source protection and ranking are unchanged.

The [experiment instructions](../../probes/admission/README.md) describe public API fixture
construction, actual pinned dsh/pi SDK generation, strict visible-output scoring, independent
durable stream readback, bounded real-vector indexing, concurrent query matrix, phase
diagnostics and observed Windows process-tree resources. Probes use synthetic data only.
External manifests bind policy, fixture, probe, Core and model artifact identities.

Actual question text reaches the native host without expected answers or source alias maps.
Only complete permitted fact timelines are explicitly seeded through Core APIs; partial
contexts remain raw evidence. Unsupported native temporal controls are not added through
fixture-only metadata. This measures the present host behavior rather than a new extractor.

## Recorded results

| Dimension | Actual observation | Boundary |
| --- | --- | --- |
| Storage | 136/136 | Public Core synthetic original/projection checks |
| Retrieval | 60/60 | Frozen canonical Core retrieval checks, not semantic recall at scale |
| Temporal | 108/108 | Explicit Core fact timelines and bitemporal queries |
| Automatic | 21/60 canonical outcomes match | Existing rules processor only; all 60 collected outcomes conservatively map to review, no automatic accepted profile |
| dsh host | Final: 7 passed, 217 failed, 0 missing | All 224 observed; actual local Qwen |
| pi host | Final: 10 passed, 214 failed, 0 missing | All 224 observed; actual local Qwen |

There are no actual automatic acceptances in this collection, so acceptance precision is
undefined. Thirty-nine canonical outcome mismatches, including blocking cases, retain the
quality gate. A conservative collector fallback is not evidence of model recognition.
The earlier partial host snapshots are separately retained and excluded from these final counts.

dsh's complete campaign independently verified replay for all 224 published streams, with
zero native case errors and zero network attempts. This establishes delivery/replay, not
correct answers: only 7/224 match the frozen canonical answer/source contract. A separate
field diagnostic counts 212 answer-text, 195 source-set and 51 decision mismatches across
224 observed host cases; those counts overlap and do not classify factual correctness.
The combined
588-check report has 82 canonical blocking failures (including automatic cases), while the
finite forbidden-literal scan found zero violations. Those blocking mismatches must not be
described as 82 secret disclosures or as evidence of arbitrary secret safety.

dsh pre-provider memory p50/p95 is 3447.707/3984.582 ms; first reliable native-visible
callback p50/p95 is 12161.735/20825.906 ms. All 224 have measured timings. Provider startup
and model warmup are separate. The 9723.926-second resource campaign sampled 38818 times,
with 29 process-memory query failures and observed aggregate peak **8,825,020,416 bytes**.
The stable root model process alone peaked at **8,541,683,712 bytes**, already above 2 GiB.
Sampling is not a hard peak bound; query failures are not model-generation errors.

Pi likewise independently verified all 224 stream replays, with zero native case errors and
zero network attempts. Its complete report has 10/224 host contract matches, 82 combined
canonical blocking failures and zero finite forbidden-literal violations across 588 payloads.
Memory preparation p50/p95 is 3344.036/3903.694 ms; first reliable native-visible callback
p50/p95 is 14791.526/24864.724 ms. Boot p50/p95 is 3872.141/4680.374 ms and request elapsed
p50/p95 is 39679.227/104605.168 ms. All four fields have 224 measured timings and no missing
timings. The 12150.640-second campaign has 48513 resource samples, 31 sampling-query failures
and observed aggregate peak **10,237,632,512 bytes**; the stable root alone peaks at
**10,106,503,168 bytes**. Resource, latency and strict canonical quality gates fail.

The chat model was preloaded once before user requests in each campaign. Initial integrity /
module import / pipeline load measured 878.751 / 70.619 / 2064.943 ms for dsh and
985.634 / 80.140 / 2273.936 ms for pi. These are single startup observations, not p95 or
per-request host bootstrap. Every generation also rechecks the pinned artifact hashes.

| Local model phase (224 calls each) | dsh p50 / p95 ms | pi p50 / p95 ms |
| --- | ---: | ---: |
| Per-call integrity | 1056.807 / 1198.989 | 935.605 / 1206.444 |
| Generation | 30137.729 / 68804.619 | 34789.607 / 99283.441 |
| First model delta | 7245.177 / 15540.745 | 9999.097 / 19486.876 |

The model's first delta is not acknowledged native publication. Generation and publication
can overlap; these phases and independent p95 values must not be added or subtracted to
invent isolated publication overhead.

The successful first dsh pilot measured about 2.67 seconds before provider dispatch;
one pilot is not p95. Corrected pi pilots also completed durable publication and replay.
Earlier pilot, excluded partial and final campaigns remain separate; no failed threshold is
relaxed on the basis of a successful stream-delivery check.

The complete 100,000-original ingestion took 3147.629 seconds. Every submission latency was
retained: p50 33.404 ms, p95 52.135 ms. Four selected originals round-tripped exactly through
public read. Up to its final submission, ingestion observed peak commit about 22.7 MB and runtime/evidence size
138,105,594 bytes. These are ingestion-process measurements, not model/whole-product totals.
The million-original target stopped at its frozen preparation budget: **426000** actual
originals in **14408.872 seconds**, `ingest_time_budget`. Submit p50/p95 is
32.978/50.902 ms across all 426000 calls. Public post-run status matches that count, and
sources 0, 1, 213000 and 425999 round-trip exactly. The configured semantic space reports
`not_built`, actual stored vectors 0 and 426000 pending messages. This is not a complete
million fixture; its target-size cold/warm/query/rebuild matrix is **unmeasured**, not filled
with extrapolated or differently shaped data. The ingestion memory snapshot up to the final
submission observed peak commit 28,569,600 bytes; final statistics/report construction is not
included in that snapshot and is not a whole-product peak guarantee.

The 100k bounded index
stage ended after 1803.006 seconds with `index_time_budget`: actual vectors/indexed messages
10016/100000, lexical messages 8500/100000. Both indexes remain behind, so admission is blocked.

The pure-semantic probe used those actual 10016 vectors and scanned 10000 fragments. The older
target was not retrieved and the result was empty, with `semantic_scan_limit`, `index_behind`,
`semantic_behind` and `evidence_state=incomplete`. Recall took 17027.789 ms. This verifies the
bounded-scan disclosure, not full-history absence or a successful full-history recall.

The matrix was intentionally stopped after **24846.499 seconds** (about 6 h 54 min).
There were 115 completed process attempts, zero duplicate attempt identities and **403/480**
expected observations. Of the 77 missing observations, 31 came from two initialization
failures and 46 were interrupted/unmeasured at operator stop. Missing observations are not
filled by retries. All 403 observed contexts have incomplete coverage; none passes the
complete-memory latency gate. The stopped summary retains per-call phase distributions.

| Condition / query | Warm n/30 | Fresh n/10 | Warm context p95 ms | Fresh context p95 ms |
| --- | ---: | ---: | ---: | ---: |
| idle / recent | 30/30 | 10/10 | 33965.240 | 30934.295 |
| idle / historical | 30/30 | 10/10 | 33011.282 | 33056.668 |
| idle / no_answer | 30/30 | 10/10 | 16989.108 | 19924.228 |
| idle / popular | 30/30 | 10/10 | 24791.987 | 24292.895 |
| writing / recent | 30/30 | 9/10 | 37572.871 | 40187.212 |
| writing / historical | 30/30 | 10/10 | 56129.171 | 53084.584 |
| writing / no_answer | 30/30 | 10/10 | 66634.010 | 78796.223 |
| writing / popular | 30/30 | 10/10 | 24172.420 | 24991.759 |
| rebuilding / recent | 30/30 | 10/10 | 100973.518 | 102642.767 |
| rebuilding / historical | 30/30 | 10/10 | 112731.050 | 135771.263 |
| rebuilding / no_answer | 0/30 | 4/10 | unmeasured | 127458.143 |
| rebuilding / popular | 0/30 | 0/10 | unmeasured | unmeasured |

These are elapsed calls with incomplete coverage, not complete retrieval. Other recorded
experiments shared the machine; idle is matrix-local. Concurrent captures grew the runtime
from 100000 to **815923** originals (715923 successful captures, zero recorded background
errors). Rebuilding used that larger fixed population. Per-sample source counts identify the
actual size; comparisons across conditions also contain population growth and do not isolate
one workload's causal effect.

After stopping only the verified owned process tree, public status still reports 815923
originals; four baseline sources round-trip exactly and the final concurrent source remains
present. Actual vectors/indexed messages are **20160/815923**, lexical indexed messages
**63493/815923**. Coverage remains behind. An immediate identity-checked process query found
zero survivors from the stopped owned tree. This is process-stop recovery, not physical
power-loss acceptance.

The matrix resource wrapper recorded 99236 samples, 113 memory-query failures and an observed
owned-tree peak **849674240 bytes**. The final logical disk snapshot is **4009206313 bytes**,
charging all external m81 roots, reused chat-model assets, the main Python environment and
harness Node dependencies. Hardlinks are counted again; global system/runtime/cache folders
are excluded. This is not peak physical allocation or a hard peak-memory bound. The native
chat-model resource failures above remain blocking.

The original runner had per-batch timeouts but no overall operator deadline. Waiting for this
already-failed diagnostic also delayed implementation delivery. The monitor now defaults to a
900-second total operator budget and accepts `OUTPUT/STOP`; either control attempts to stop its
owned command tree and records a nonpassing partial outcome. Termination failure retains
`stop_error` and root-exit status; it cannot imply that all descendants stopped. Longer campaigns
require an explicit budget. These controls do not alter the frozen policy, expected sample
counts or original observations. Original probe bytes are archived separately. Three real
Windows process tests verify deadline, descendant termination, operator stop and failure-receipt
preservation (3/3, 12.095 seconds); strict mypy now covers 103 files.

An explicitly separate diagnostic while the 100k index was incomplete measured lexical search
2750.760 ms and context construction 41254.897 ms, degraded with `index_not_built` and
`semantic_behind`. The profiled context included retrieval/fusion, embedding, source checks,
auditing and target tokenization; nested inclusive phase times must not be added.
It does not represent an indexed steady state or an isolated idle machine.

The standalone pinned MiniLM diagnostic reported integrity hashes 138.619 ms, import
95.764 ms, model load 772.248 ms and inference 7.391 ms, with dimension 384 and zero blocked
network attempts. It is a separate startup diagnostic, not a subtraction from Core latency.

The synthetic MCP echo baseline completed 10 fresh-process and 30 warm samples:
roundtrip p50/p95 2.371/3.740 ms fresh, 1.452/1.983 ms warm. Bootstrap is excluded
from those roundtrip numbers. The actual memory process bootstrap/handshake took 2374.102 ms
and its first status roundtrip 779.540 ms in this separate diagnostic. These measurements
share the machine with the recorded experiments and must not be subtracted from each other.

Canonical mismatch does not always mean a wrong fact: g01-q1 returns the same Java17 value
and correct source, but a full sentence fails the frozen canonical-value contract. Separately,
public source reads and the actual provider evidence section confirm that g01-q6, g01-q8 and
a01-q cite their current user question, captured as `input:0:0`, as historical memory. That
observed recirculation is one concrete failure path, not proof of the cause of all mismatches.
The frozen payloads and scorer remain unchanged.

Measured follow-ups are [#83](https://github.com/destinyTaoTao/evernook/issues/83)
(repeated target-tokenizer worker startup) and
[#84](https://github.com/destinyTaoTao/evernook/issues/84)
(bounded/cancelable retrieval when indexes lag) and
[#85](https://github.com/destinyTaoTao/evernook/issues/85)
(current-input versus historical-evidence scope) and
[#86](https://github.com/destinyTaoTao/evernook/issues/86)
(pinned Qwen host resource diagnosis) and
[#87](https://github.com/destinyTaoTao/evernook/issues/87)
(new-process initialization under concurrent writes/maintenance). All are `needs-triage`; none is an
assumed framework replacement or an unreported quality/budget reduction in this ticket.

## Evidence and validation boundaries

Current external evidence lives under
the external evidence root's `evernook-m81-*` directories. Each run manifest records its
actual paths and identities; personal runtime paths are not needed to reconstruct the commands.
Canonical core and automatic observations are in `core-01` and `automatic-01`;
the complete scale fixture is `scale-100k-02`, and `scale-1m-02` is the stopped, incomplete
million-target fixture. Its post-run public reconciliation is `1m-target-reconciliation-01`.
Earlier failed pairing/pilot directories are preserved and excluded from formal results.
`dsh-quality-01` and `pi-quality-02` are the completed native campaigns, with separately
reconciled `dsh-summary-final-01` and `pi-summary-final-01` reports.
The partial `pi-quality-01` run was stopped after verifying owned process identities;
it is diagnostic evidence only. Fresh independent fixtures prevent prompt/output contamination.

Two read-only review axes found the million fixture's recent target was historical and that
failed background work could be labelled as a valid concurrent condition. Both were corrected:
recent targets now follow the fixture's expected scale and timestamp; each cell records actual
background progress/errors and invalid workloads cause a nonzero outcome. The separate IPC
echo diagnostic establishes only transport/protocol baseline; production roundtrips include
auth/audit/Core. Retrieval/fusion is reported at public `recall` scope, not as an invented
isolated RRF-only timer.

Full Python **408/408** passed (1492.124 seconds); SDK **85/85** passed (713.310 seconds),
no skipped/canceled checks. Source, tests and experiment scripts passed strict mypy (103 files);
TypeScript typecheck/build passed. Receipt summarizers passed actual partial-matrix smoke
runs, preserving startup failures and missing attempts. These runs shared the machine with
the recorded experiments. Both review axes' stop-receipt race finding is repaired and rechecked.

The writing/recent cold process 5 failed initialization with `unsafe_runtime_path` before
querying. Later, the rebuilding/no-answer warm batch failed initialization with `storage_busy`
after 6170.517 ms, before producing any of its 30 expected samples. Those missing observations
remain missing; no retry overwrites either failure. Their common cause is unproven. The #82
deliberate failed-commit lock probe does not establish a repair of these startup paths.
Two review axes' concrete recent-target, no-op workload and missing-timing findings are fixed.
Native host reports are complete; the stopped scale campaign remains partial. No complete
million-target matrix, complete-memory latency pass, full-history semantic coverage, physical
OS disconnection, desktop UI or physical power-loss acceptance is claimed. The installed
candidate is separately bounded in [M58-24](m58-24-installed-profile.md).

## Core0.1.12冻结本地质量与索引模式补验（2026-11-09）

在新的仓库外合成夹具复跑当前非editable Core0.1.12；运行前后45个代码成员与Git/wheel/实装逐字节相同，wheel SHA-256为`3face086b7e877953d5b3891e58d3f25edc60dbc46863c2dd39f30ed06bffa6e`。原suite身份`b487e1777bd06eef30a50c3fc6811ef54e801c8bc00b3d46e00c6388b8d9886b`、原policy文件和计分器保持，语料仍是委派AI复核版，人工复核pending；没有运行模型或重新采集dsh/pi回答，也没有将历史宿主结果合入这次新评分。

| 当前实际分项 | passed / failed / missing | 边界 |
|---|---|---|
| 保存 | 136 / 0 / 0 | 公开submit/read、原文/受保护投影 |
| 字面检索 | 60 / 0 / 0 | 原冻结exact/lexical查询，不是语义召回 |
| 时态 | 108 / 0 / 0 | 显式事实种子，不是自动抽取 |
| 自动规则 | 21 / 39 / 0 | 60个独立允许上下文，全部实际为review；候选和接纳画像均0，处理错误0 |
| 宿主回答 | 0 / 0 / 224 | 本批未运行，保持missing |

当前合并364份真实观察后，325通过、39失配、224缺失，outcome=failed；27个canonical阻断项保留。有限canary扫描仅覆盖本批已提供观察，违规0，不是未知秘密零泄露保证。自动规则期望accept12/review21/reject19/clarify8，而实际均review；无真实接纳，precision未定义，不能把保守回退当作拒绝/澄清识别成功。该结果与旧有限规则分项相同，不因Core查询优化而放宽门槛。

新增`quality.py lexical`可复跑阶段：在新的136来源公开API夹具，按未建、旧格式落后10/136、旧格式追平、仅覆盖、两项加速、仅FTS ID、反向恢复旧格式依次核对。每种60题均独立计分60/60，完整命中/片段/顺序/分页及来源计数/水位与未建状态相同；共420次重复模式观察，不是420个独立问题，也不是重建竞争或100k/1M矩阵。每模式原始页和评分分别保留，任一模式失败即保存进度并报错，根结果只含最后模式60条检索观察、其他分项仍missing，正常退出2。

接入后用真实安装包再跑新阶段，7模式完整通过；另做一次探针负向对照，故意只破坏behind模式的一段返回片段、保持来源ID。冻结来源评分仍60/60，但完整页守卫在第2/7模式明确失败，后续未执行，错误页和进度保留。它是验证探针检错能力的注入，不是Core真实损坏。新probe SHA-256 `0ebccaf4bd7f18a830379d9c5b298f4b77b845a9a033efd286a5b1eefe6fa331`；既有save/seed/automatic/prepare函数AST未变。strict mypy该文件通过；首次未配置源码搜索路径出现import-untyped，按项目源码设置MYPYPATH后通过，没有忽略类型错误或给安装包补标记。

资源monitor均在原上限内自然结束：Core7.270秒/29采样/观察峰86466560字节/退出2（另284项未运行）；automatic40.067秒/160采样/峰89825280/退出1（39失配），3次进程采样查询失败保留；先行模式探针5.769秒/23采样/峰23330816/退出0；接入后的正式脚本6.272秒/25采样/峰87977984/退出2；负向对照捕获预期错误3.518秒/14采样/峰72622080/退出0。除automatic的3次采样失败外，其余0。PowerShell对非零子进程的外层汇总退出1不覆盖monitor记录的实际2/1；不将观察峰称作硬峰值或p95。

后验复算合并评分/全部模式页和原始资源样本、61份Core/自动夹具DB quick_check/FK通过，接入阶段的单独后验及负向对照核对通过，相关Node/Python进程0。冻结脚本/语料/清单、原始观察、完整页、失败、后验与清理在`Documents/Codex/2026-11-09/evernook-81-quality012-01`保留；未提交运行库或访问材料。本批仅增加验收探针与记录，无Core/插件/模型策略变更，不改日用profile。完整宿主质量、语义/规模/正式时延和物理OS门禁仍开放；新检查点持久格式及ORT原生提案未接入，#105等待原生计数，PR93保持draft。
