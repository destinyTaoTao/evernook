# #84 — bounded lexical fallback

Scope: small public Core/context/MCP regressions before long-scale admission. The public
search API now accepts a finite positive `timeout` (default 30, maximum 120 seconds) and
an optional in-process cancellation event. Interrupted queries fail explicitly; no incomplete
scan is returned as a complete empty history or an unstable partial offset page.

The same budget reaches SQLite VM progress and Unicode matching inside a large original.
Recall passes its remaining budget to all three lexical channels and preserves its
`recall_timeout` / `recall_canceled` errors. Context reports incomplete coverage. Best-effort
audit writes no longer wait on a writer lock; ordinary writes keep their existing transactions
and busy timeout. This is cooperative cancellation of SQL/Python scanning, not preemption of
arbitrary OS I/O or a hard-real-time guarantee for every other context phase.

The red regression used one 960000-byte synthetic original: a 10 ms budget previously returned
a complete empty result after scanning it. It now fails at the scan boundary. Real SQL-VM
fault workload, in-flight cancellation and the next successful query are also covered.
The Python MCP test transport explicitly selects this checkout's source, avoiding accidental
imports from another editable checkout.

Before/after used the same original, interpreter/hardware and public `SemanticRecall` 10 ms
budget, three observations each. Baseline `13c3545` exited with `recall_timeout` after
172.220 / 163.353 / 170.181 ms; fixed source exited with the same error after
19.244 / 20.511 / 21.399 ms. Owned-tree sampled peaks were 84574208 / 87232512 bytes;
whole probe windows were 1.261 / 0.763 seconds. These are responsiveness diagnostics with
the same unsuccessful recall outcome, not successful recall speedups or formal p95.

Validation: search/context/semantic and real MCP timeout regression **42/42** pass in
40.106 seconds under an explicit 120-second test-run budget. Earlier targeted 18/18 passed;
strict mypy source/tests passes 94 files. Existing exact text, filters, paging, lag/rebuild
coverage, source controls and semantic fallback regressions remain in that run.
Receipts: `evernook-followup-84-small-01/summary.json` and
`evernook-followup-84-validation-02` under the external evidence root.

Full 100k/1M cold/warm/concurrent comparisons remain deferred. Semantic/status and other
non-lexical context phases are not claimed as universally preemptible. No quality/resource
threshold or original #81 observation changed; daily model admission remains blocked.

Spec review caught one remaining CPU boundary: one valid 160001-byte combining run spent
about 0.95 seconds in an indivisible NFKD reorder despite a 10 ms check callback. A public
search regression independently failed its 0.5-second bound (0.974 seconds). Long groups now
decompose individual code points and stably order non-starters by combining class with budget
checks, following [Unicode normalization](https://www.unicode.org/reports/tr15/). Short groups
keep the original implementation. Both the bounded-search regression and independent
normalization comparisons (leading marks, compatibility ligatures, Hangul and case folding)
pass; the pinned normalization identity and source coordinates remain unchanged.

Final run after that repair: **44/44** pass (42.430 seconds), within the same 120-second
operator limit. Spec re-review confirms the combining-run finding is resolved.

Standards review found no blocking rule violation; a small correction to #87's evidence
description is included explicitly to avoid claiming marker writes were real capture.
