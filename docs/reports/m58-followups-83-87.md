# #83–#87 bounded follow-up delivery

Authorized order: **#87 → #84 → #83 → #85 → #86**. Each has a separate commit pushed
to main. No 100k/1M matrix or 224-question campaign restarted; no budget or scorer relaxed.

| Ticket / commit | Delivered | Remaining acceptance |
| --- | --- | --- |
| #87 `108bf37` | Initialized WAL/identity read snapshot avoids startup writer lock | Historical unsafe_runtime_path not reproduced; full frozen concurrent load |
| #84 `4f79fb9` | Deadline/cancel reaches SQLite and long Unicode fallback | 100k/1M and formal p95 |
| #83 `fe9dc3b` | Scoped verified tokenizer, exact counts, cleanup; ticket closed | Overall admission remains in other tickets |
| #85 `75941ef` | Current receipt excluded before historical limits/profile allowance | Full frozen model-quality comparison |
| #86 `a486500` | Process/heap/ORT-output and release/rebuild diagnosis | Fixed Qwen still exceeds 2 GiB; varying-input/native allocation attribution |

Reports: [startup](m58-followup-87-startup.md), [query budget](m58-followup-84-query-budget.md),
[tokenizer](m58-followup-83-tokenizer-session.md), [input scope](m58-followup-85-current-input.md),
[resources and actual Qwen replay](m58-followup-86-model-resources.md).

## Verification

- #87: 21 targeted tests (36.786s), plus held-writer before/after and actual concurrent
  capture/FTS/MiniLM/read smoke. Path/ACL checks remain intact.
- #84: 44 targeted tests (42.430s), including long combining marks and SQLite interruption.
  Small timeout response 163–172ms →19–21ms is responsiveness, not formal recall p95.
- #83: 8 real tokenizer/session/owner-exit tests (4.012s). Same 24 counts 7.508s→0.613s;
  full small context 2.614s→0.634s. Payload/1904 tokens match the independent host.
  Sampled peak rises 183144448→294051840 bytes; tradeoff is explicitly recorded.
- #85: 26 Core/context/semantic tests (27.107s), plus semantic-only/profile/MCP regressions;
  4 new real SDK cases (13.811s) and 8 existing budget/protected-input/cancel/replay/control
  cases (42.164s). External editable environment resolves this checkout.
- Combined strict mypy source/tests/admission/installed probes: **110 files**. Node
  typecheck/build passes. Windows CLI alternate-casing refusal verified before data loading.
- #86: three 100-second-cap diagnostics finish in 59.106/61.610/70.627s, all six token/text
  outputs equal. Arena-off sampled peak 5.45GB→4.07GB is slower and still fails 2 GiB;
  it is not promoted as default repair.
- Real dsh/Qwen g01-q6 finishes in 25.065s under 100-second cap. Public current receipt,
  complete native prompt, absent self-ID, first-visible independent read and final durable
  replay verified. Strict canonical sentence-format failure remains; one case is not a new
  full quality score. Details and independent-fixture UUID limits are in the #86 report.

Full Python contracts and full Node SDK suite were each attempted **once**, with a 180-second
operator cap. Both are **incomplete**. Python stopped at 181.501s without a final summary.
SDK observed 22 passes before its 181.069s stop; no final summary means no full pass.
SDK termination recorded `CalledProcessError` against the changing process tree; that original
receipt does not prove termination. Later separate process snapshots found the root and sampled
process identities gone; no unrelated PID was killed. The stop-error receipt is preserved.
No long suite was restarted; old #82 full-suite results are historical.

## Standards

Fresh reviewers: #87 no required change; #84 no blocking standards issue; #83 contradictory
worker-lifecycle documentation fixed; #85 no actionable issue; #86 Windows case-sensitive
output guard fixed with physical parent/checkout paths and relative containment.

## Spec

Fresh reviewers: #87 writer-lock repair supported, historical path/full-load partial;
#84 combining-mark deadline bypass fixed; #83 polling-completion race fixed; #85 current-derived
profile budget crowding fixed with red/green regression; #86 no other defect, diagnostic limits
retained. Remaining acceptance above is not completed or counted as model admission.

## Handoff

Use bounded native allocation attribution next, or schedule remaining scale/path/model-quality
acceptance as a separate campaign. Do not resume old matrices or mix installed/source Core.
#82 wheel/npm identity belongs to `13c3545`; newer source needs a new installed candidate.
Managed branch: `codex/m58-followups-83-87`. Original main's five unrelated design drafts are
preserved, not committed. External evidence: `Documents/Codex/2026-11-04/evernook-followup-*`.
No credential, runtime database, model asset or raw host trace is committed.
