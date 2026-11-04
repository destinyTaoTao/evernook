# #86 — independent last-hidden LM-head candidate

User-authorized continuation from `fc7c47a`: verify a separate export without reducing full
input or output allowance. The candidate remains **experimental and blocked**; no model,
provider, frozen policy or daily profile is replaced. Original assets are unchanged.

## Export and identity

Parent Qwen3-0.6B q4 graph SHA-256:
`d43d836fc5e240df9013733ccd214972c5d21bd9ec47e574e4f1e359cf90aed0`.
Candidate graph SHA-256:
`e9370b411c56b37bfeb48f2787655c62b014eb17d8fb441d3fc281ef0d01e3ce`.

`export_last_head.py` uses isolated ONNX 1.20.1 tooling (Python 3.14.4, protobuf 7.36.2,
NumPy 2.5.4); none is added to Core's runtime dependencies. It inserts `Slice [-1:]`, axis 1,
immediately before `/lm_head/MatMul`, adds four int64 constants, and updates only the logits
sequence dimension to 1. Undoing precisely those changes restores the complete original
serialized graph hash, including every original initializer/node/attribute. Eight tokenizer
and config artifacts are copied unchanged. [Slice semantics](https://onnx.ai/onnx/operators/onnx__Slice.html)
preserve the complete upstream hidden sequence; slicing happens before projection.

The original fused `SimplifiedLayerNormalization` is unsupported by the standard ONNX
checker. The first export attempt failed explicitly on it. The successful exporter checks
the new Slice and records identical parent/candidate full-checker failures, then relies on
exact structural restoration plus actual ORT loading. It does not claim full standard-ONNX
validation. Successful export: **7.772s**, 60s cap, separate build peak **3604983808 bytes**;
that is tooling memory, not an inference admission result. Failed/partial export-01 remains
separate; only completed export-02 has its final profile/manifest.

The experimental loader requires distinct profile/model identifiers and a hard-pinned graph
hash; all nine assets are verified at load and every generation. Original-as-candidate,
candidate-as-original and tampered-config checks all refuse before inference. Public startup
metadata cannot mutate the private expected hashes.

## Native prefill and complete generation

Same machine, Node 24.12.0, Transformers.js 4.2.0 / ORT 1.24.3, CPU 4/1 threads. Same captured
synthetic provider request: **1321 complete input tokens**, 32768 window, original 4096 memory
budget and **512 maximum new tokens**. Prompt/protocol/attention/KV and weights stay unchanged.

- All input-feed hashes match. All **56 KV outputs match byte for byte**.
- Logits become `[1,1,151936]`, **607744 bytes**, instead of `[1,1321,151936]`, 802829824 bytes.
  The candidate logits hash is exactly the original final-row hash:
  `f64423a9d9bc5dc2bcd53d7dcffb82bfd5a6e8471f05e5d1ffd163ca4950c6d6`.
- Candidate first greedy token remains **515**. Two complete generations naturally finish at
  **119 tokens** each, with the entire token sequence and final text identical to the parent.
  No token cap reduction, early JSON termination, gold-driven payload or scorer change.

| Diagnostic | Parent | Candidate |
| --- | ---: | ---: |
| Same-request prefill observed tree Private Bytes peak | 5248839680 | 3215478784 |
| Prefill process window seconds | 15.307 | 16.052 |
| Two complete generations observed tree peak | 5453262848 | 3815657472 |
| Two-generation process window seconds | 59.106 | 60.872 |
| Pipeline load ms | 2168.734 | 2217.712 |
| First generation ms | 27105.916 | 27093.154 |
| First model delta ms | 7049.732 | 5619.790 |

These are small diagnostics, not p95, overall admission or guarantees. Cache/load/scheduler
conditions are uncontrolled; no overall speedup is claimed. Native-stage windows include
profiling/GC/hashing; complete generation is measured separately. Every row includes existing
integrity and process costs in its recorded window. 250ms sampling can miss peaks. Runs above
have zero query failures; individual caps are 35s prefill and 90s for the two-generation batch.

## Real dsh, independent replay and cancellation

A fresh single g01-q6 public-API fixture with the candidate runs through the actual dsh SDK,
full request counter, input capture and durable publication. Normal case: **26.317s** under
60s cap, observed tree peak **3349774336 bytes**. First visible content independently reads
back and final stream replay matches. The unchanged strict canonical answer check still
fails on sentence formatting; the original quality failure is retained. New fixture UUIDs
and candidate counter identity change native prompt metadata, so this is a path regression,
not the byte-identical input comparison above or a new 224-case quality score. Pi/desktop
candidate acceptance is not claimed.

The first cancellation experiment exposed a wrapper defect: publication correctly stopped
at two visible characters, but the background generator still completed **78 tokens**.
The explicit provider-aborted check failed, and that original receipt is preserved. This is
separate from the graph transform. The experimental wrapper now marks its consumer closed
before awaiting its background generation; the next token callback terminates it. Already
running native kernels are not preempted by this callback mechanism.

After repair, the actual dsh diagnostic stops at **2 generated tokens**, records `AbortError`
with `aborted=true`, and preserves exactly the visible prefix in terminal `canceled`,
`complete=false`, `final_message_id=null`. It finishes in **13.797s**, 60s cap, peak
**3073548288 bytes**. Cancellation observations remain explicit diagnostics, never successful
model answers. Fresh MCP processes after host cleanup replay both normal/canceled streams
and repeat-read identical pages. A same-model-instance cancel-then-resume check subsequently
generates all **119 parent tokens/text exactly** after cancellation; **29.568s**, 60s cap,
peak **4083449856 bytes**. That higher reuse-scenario peak is retained, not replaced by the
best prefill number.

## Outcome and evidence

The graph change removes the unnecessary full-sequence LM-head projection and JS output
copy for this request, with exact observed equivalence. Memory improves but **still exceeds
2 GiB**, including the cancel/resume scenario. The candidate is not promoted; broader inputs,
generation modes, varying-input retention and formal quality/resource admission remain open.
The next investigation can address dense/shared weights and KV/output ownership separately,
with fresh bounded evidence; no such extra change is bundled here.

Node typecheck/build, strict source/tests mypy (94 files), isolated strict exporter mypy,
three loader refusal cases, actual model-stage/full-generation/native/cancel/resume and
fresh MCP replay checks pass. No full suite or 224-case/scale campaign is run in this slice.
Real healthy generation was rechecked after the cancellation-wrapper fix through the
same-instance resume experiment. Full generated output remains governed by the old scorer.

External evidence: `Documents/Codex/2026-11-05/evernook-86-last-head-*`, including export
manifest/profile, prefill/reference final-row comparison, two-generation runs, normal SDK,
failed cancellation-01 and repaired cancellation-02, resume, negative cases and fresh replay.
Source/version snapshots in each manifest preserve intermediate probe revisions. No model
binary, raw prompt, runtime database or credential is committed.

Exporter CLI checks also refuse the wrong parent graph and checkout-contained output before
creating a candidate. All five original unrelated design-draft hashes remain preserved.

## Standards

Fresh read-only review finds no actionable documented violation or design smell. External
paths, fixed candidate identity, source preservation and explicit failure boundaries pass.

## Spec

Fresh read-only review finds no actionable spec discrepancy. The independent candidate
keeps original weights/upstream data and limits, with observed
prefill/full-generation/native/replay/cancellation equivalence as documented. Native 2 GiB,
full quality and broader-input acceptance remain open; no promotion is claimed.
