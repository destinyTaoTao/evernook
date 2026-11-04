# #87 — initialized runtime startup under a writer

Baseline: `13c3545`. Scope: public `MemoryRuntime` constructor/read with a real SQLite WAL
writer, using synthetic data. No daily profile or model admission changes.

The one-second red regression held `BEGIN IMMEDIATE` with the configured busy timeout zero.
Opening a second, already initialized runtime failed with `storage_busy`: every constructor
unconditionally entered schema-install writer transactions. The fix first reads a consistent
snapshot of version, required schema objects, WAL mode and key-bound ledger identity. A complete
v5 ledger opens without schema writes. Missing objects, older versions or missing identity still
take the existing installation path. Future versions, foreign identities, NTFS/path and ACL
guards remain enforced. Writes can still return a diagnostic `storage_busy`.

Five open/read samples per condition used the same single-original fixture, baseline source
snapshot and current source, Python 3.14.4 and the existing Windows machine:

| Condition | Baseline exact reads | Fixed exact reads | Baseline process wall s | Fixed process wall s |
| --- | ---: | ---: | ---: | ---: |
| No concurrent writer | 5/5 | 5/5 | 2.453 | 2.311 |
| Held WAL writer | 0/5 (`storage_busy`) | 5/5 | 2.328 | 2.346 |

Per-open/read observations were 284–321 ms baseline without contention and 285–305 ms fixed.
Observed process-tree peaks were 85,680,128/84,942,848 bytes without contention and
84,611,072/81,641,472 bytes with the writer. They include bootstrap/ACL children and are sampled,
not hard peaks or formal p95. The improvement established here is successful read startup under
contention, not a material unloaded-latency or large-scale performance claim.

A separate bounded WAL-lifetime diagnostic completed 20 public constructor/read attempts
while a synthetic marker writer committed 370 times: all originals matched and no startup
failure occurred. This **did not reproduce** the historical `unsafe_runtime_path` event and
does not prove that event is impossible; path protection was not weakened.

Validation: startup, boundary, recovery and legacy important-message tests **21/21** pass
(36.786 s); strict mypy source/tests **94 files** pass. Public regression includes future-version
and foreign-ledger rejection, and exact original/idempotent retry after releasing the writer.
External receipts: `evernook-followup-87-small-01/summary-02.json`, `wal-lifetime-01.json` and
`evernook-followup-87-tests-01.log`. The first baseline setup was rejected as inside its source
snapshot and is excluded; the corrected sibling source/runtime layout is recorded separately.

Full frozen 100k/1M replay is deferred per the small-test-first delivery scope. The historical
path-resolution failure remains unexplained. #81/#82 model, scale and OS admission gates remain.

Additional bounded integration: five public constructor/read calls overlapped actual FTS and
pinned MiniLM refreshes on five synthetic originals. Two refreshes published two real vectors
each; all five original reads matched, with no reader or maintenance errors. Receipt:
`maintenance-smoke-01.json`. The capture step preceded this first overlap. A second bounded
run (`concurrent-maintenance-02.json`) overlapped eight successful real captures, progressing
FTS/pinned-MiniLM maintenance and five exact new-reader results, with no errors. The earlier
WAL-marker diagnostic alone does not establish actual concurrent capture.

Standards review: no required changes; one nonblocking reminder to keep the readiness object
list synchronized with schema installation. Spec review: known writer-lock repair supported;
historical path failure and full frozen-load replay remain partial requirements.
