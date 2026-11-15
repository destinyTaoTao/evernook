# Autonomous performance and quality validation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Complete reproducible automatic validation and fix measured bottlenecks without weakening evidence, privacy, durability or frozen admission gates.

**Architecture:** Keep existing one-event APIs and defaults. Add a bounded explicit Python bulk-ingest operation with one durable transaction, and bounded local embedding sessions that reuse verified assets and the existing offline worker. Extend diagnostic collectors rather than inventing observations for missing lifecycle/model phases. Store all fixtures, raw results, models and access files outside the checkout.

**Tech Stack:** Existing Python 3.14, SQLite WAL/FULL, unittest, Node 24, pinned Transformers.js/ONNX and existing native diagnostic assets. No new production dependency or automatic model admission.

**Spec:** GitHub #81/#84/#120/#121/#124/#148, `probes/admission/policy.json`, frozen `eval/zh-learning-v1` and `eval/zh-reflection-semantics-v1`; existing backup, purge and provider contracts.

## Global constraints

- Preserve frozen suite/scorer/policy identities, `missing_allowed: 0`, blocking failures, 100000/1000000 targets, 2 GiB resource gate and 1000/150/50 ms latency gates. A diagnostic subset cannot pass the full gate.
- Preserve every original, keyed source identity, independent receipt and per-operation permission check. Never reduce synchronous=FULL or expose protected text to an embedding/model.
- No real dialogue, user-runtime mutation, cloud fallback, paid API invocation, model download, default-profile change or speculative schema migration.
- Preserve unrelated working-tree changes. Reuse the current branch; one related commit per ticket; push verified commits and append actual results/limits to issues.

## Review focus

1. A conflict or cancellation midway through a bulk request must publish none of its new originals/jobs.
2. Bulk retries and a concurrent single writer must retain source-idempotency semantics, including protected projections and purged-source guards.
3. Worker reuse must reject modified model assets, mismatched replies, oversized output and protected input; cancellation/close must reclaim owned processes.
4. Restore, processor re-admission and confirmed purge observations must execute real public protocols and preserve aliases/controls; never fill missing judgments from expected answers.
5. Scale and quality campaigns must retain source count, actual vector coverage, process/resource limitations, complete/partial coverage and separately labelled workload/timing boundaries.

## Task 1: Bounded durable bulk ingestion (#84)

**Files:** `src/evernook/runtime.py`, new `tests/contracts/test_submit_many.py`, `probes/admission/scale_ingest.py`, relevant contract/report.

**Interface:** `MemoryRuntime.submit_many(access, events: tuple[Event, ...], *, cancel: threading.Event | None = None) -> tuple[Receipt, ...]`. At most 100 events / 4 MiB total UTF-8 text; empty/invalid requests refuse. Reuse `_validate_event` and `_persist_event`; one authorization, one ordinary write transaction, per-event source checks and audit evidence. Roll back on any failure before COMMIT; no model, maintenance or MCP permission change.

- [x] Add and run failing tests for transaction-wide conflict/cancellation, duplicate replay, single-writer collision, sensitive projections and invalid/oversized input.
- [x] Implement the bounded method; test committed originals/jobs/audit, restart roundtrip and unchanged single-event behavior.
- [x] Add explicit `--batch-size` (default 1) to scale collection. Batch mode records batch durations and receipt ranges rather than pretending they are serial per-message timings; retain the existing default.
- [x] Run labelled serial/bulk comparison and actual 100k/1M ingestion/independent receipts with resource guards. Preserve failed serial/partial results. Commit verified implementation and evidence.

## Task 2: Bounded local embedding reuse (#84/#124)

**Files:** `src/evernook/embedding.py`, `embedding_worker.mjs`, new `embedding_session.py` and `tests/contracts/test_embedding_session.py`, targeted probe and provider/service wiring only after equivalence is demonstrated.

**Interface:** `LocalMiniLM.session(*, timeout: float = 120)` context manager yielding an EmbeddingBackend with the same space and strict request/result contract. Requests carry monotonic IDs; models load once in a bounded child. Check original hashes before every request. Serialized exchanges honour waiting cancellation/deadlines; invalid output/timeout closes the session. Existing `embed` remains one-shot. Service lifetime adoption must own close and parent-death cleanup, without changing user configuration.

- [x] Add failing lifecycle/correlation/tamper/cancellation/overflow tests with a deterministic synthetic worker.
- [x] Implement sessions by reusing existing tokenizer-session and worker-lifetime patterns; keep worker networking disabled and all outputs bounded.
- [x] Compare real pinned MiniLM vectors and output nulls byte-for-byte; collect separate cold/warm sample distributions, resource sampling and in-flight cancellation/restart.
- [x] Integrate only demonstrated safe reuse; run semantic/control/service regressions and non-editable wheel verification. Commit with measured limits.

## Task 3: Close automatic quality-collector gaps (#121/#148)

**Files:** `probes/admission/learning.py`, targeted collector tests/probe, existing restore/purge/provider APIs; no frozen dataset/scorer edits.

- [x] Exercise real synthetic purge, backup/control-preserving restore and processor re-admission for the skipped raw-arm lifecycle cases; add negative protocol tests.
- [x] Remove only the diagnostic 64-source collection restriction through an explicit bounded scale mode; generate the actual frozen 10004-source position scenario using public ingestion.
- [x] Run all 33 raw-arm cases for lexical and pinned local embedding paths; preserve true failures and unrun derived/host judgments separately.
- [x] Use existing local native assets and frozen ten-case semantics collector for independently labelled protocol/model controls. Do not introduce answer-specific guards, repair answers, alter scoring or enable an unadmitted model.
- [x] Publish reproducible quality/resource tables and commit collector fixes; stop only on actual missing assets/authorization or a required product decision.

## Task 4: Actual scale matrix and final review (#81/#84)

- [ ] Validate 100k/1M source receipts/counts and actual vector rows. Run existing recent/historical/no-answer/popular cold/warm and write/rebuild conditions where preconditions are satisfied; invalid workloads and missing coverage remain failures.
- [x] Profile failed cells, apply only evidence-backed compatible fixes, and compare full pages/control states before and after.
- [x] Run relevant source/type/installed checks and one fresh-context review of the final changes; resolve actionable findings.
- [x] Update the implementation records and tracker with completed, failed and missing gates. Keep daily admission disabled; list only concrete human actions/decisions as blockers.

## Initial evidence and rulings

- Public serial ingestion reached 9000 originals in 210.68 s on a new synthetic fixture; the initial 900 s monitor is a diagnostic operator bound, not the frozen 14400 s ingestion limit.
- Four interleaved 100-submit pilots retained FULL durability. Holding an idle connection reduced observed p50 from 36.68/40.85 ms to 13.82/12.97 ms under the concurrently owned ingestion workload. This is diagnostic, not a stable p95 claim. Prefer explicit bulk transactions over hidden connection pooling because they avoid stale/nested transaction semantics and amortize commits without weakening returned receipts.
- MiniLM three-call baseline and owned-tree samples are retained in the external campaign root. No cloud credentials or real memory were read.

## Final disposition

Tasks 1–3 completed with real failures/missing dimensions retained. Task 4 remains partial: the current structure failed the complete recent 100k cell, 1M lacks full vector coverage, and the rest of the frozen matrix is unmeasured. Independent final review found no further confirmed defect after regression fixes. Job-based monitoring replaced invalid PID-parent attribution; all old resource peaks are excluded from admission. Dated central Markdown/JSON report contains 58 raw artifact SHA anchors and the concrete default-off derived-schema/backfill proposal requiring a data-migration decision under user guidance §4. This is not daily-use or model admission.
