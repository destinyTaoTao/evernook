# #86 — native prefill high-water attribution

Continuation from `f276af1`, using the same pinned Qwen/ORT/model request as the previous
[bounded diagnosis](m58-followup-86-model-resources.md). No production provider, model asset,
token budget or default profile changes. This delivery localizes the failure; 2 GiB admission
remains blocked.

## Minimal reproducer

`prefill-resources.ts` loads and verifies the existing candidate through `LocalQwenChat`.
At the first public ORT run boundary it captures the actual prepared input tensors and stops
the generation wrapper **before inference** with a diagnostic-only sentinel. It then runs
exactly one real native prefill using those feeds. This is an isolated stage experiment,
not a shortened successful answer or a 512-token generation/quality test. Context, attention
and input IDs remain complete; the original wrapper allowance is still 512.

The same captured request has **1321 input tokens**, 59 graph inputs and 57 outputs. No
`num_logits_to_keep` input exists. Two separate baseline runs reproduce all 57 output hashes;
the last-row logits' greedy token **515** equals the first token of the previous full real
generation. Native profiling is enabled identically in all variants. Measurements include
profiling/GC/hash overhead and are not formal latency or p95 samples.

Each model command has a **35-second** operator cap. Three one-variable runs, one baseline
consistency rerun and one ownership-control repair verification complete normally; no long
suite or model campaign is started. Actual windows are 14.802/15.050/16.306/14.799/17.806s.
Windows sampling is 250ms, with zero query failures. Timestamped 750ms ownership phases
allow at least two samples during the output-held/released plateaus. GC and native allocator
finalization are asynchronous; model-loaded observations can still move, so the table uses
the stable feeds-ready stage before inference. No between-sample peak guarantee is made.

| Private Bytes observation | Baseline | Prepacking disabled | Logits not fetched |
| --- | ---: | ---: | ---: |
| Observed process-tree peak | 5193433088 | 6767349760 | 4382691328 |
| Stable feeds ready | 1859485696 | 3439005696 | 1853513728 |
| Output buffers held | 5192151040 | 6766067712 | 4381421568 |
| Output buffers released | 4084469760 | 5657833472 | 4079067136 |
| Session released | 276594688 | 278462464 | 276455424 |

The stable phase values are the root Node process, not RSS or summed overlapping windows.
All matching samples in each stable window agree. Root PIDs, exact phases, sample counts,
timestamps and original stop receipts remain external. The second baseline is an independent
consistency sample, not a percentile claim.

Review found that V8 can collect a local result before the nominal held interval. Baseline-02
confirmed output hashes/first token but had only 137935 ArrayBuffer bytes in that interval;
it is **not** a valid held-buffer plateau replication. The probe now stores outputs in an
explicit global GC root for the held interval, clears it in `finally`, and records/asserts
ArrayBuffer bytes at least equal to all fetched outputs. The final rooted run (2026-11-05)
retains **1105973455 bytes**, above the required **1105835520**, then releases to **137935**.
All 57 output hashes/first token still match. Peak is **5122760704 bytes**, 71 samples, zero
query failures, normal exit in 17.806s. This verifies the ownership control and reproduces
the same attribution, not a precise cross-run peak guarantee.

## Confirmed mechanisms

1. **Full-sequence LM-head allocation.** Native profile identifies `/lm_head/MatMul` producing
   float32 `[1,1321,151936]`: **802829824 bytes**. The graph consumes all hidden positions,
   although generation subsequently selects the last row. Merely dropping logits from fetches
   does not skip this operator: the no-logits profile still records the same 803MB output.
2. **An additional native-to-JS output copy.** ORT 1.24.3's CPU binding allocates a fresh
   ArrayBuffer and copies the numeric OrtValue into it. Total fetched prefill outputs are
   **1105835520 bytes**: 802829824 logits plus 303005696 KV. The measured JS ArrayBuffer
   increase agrees. Omitting only the logits fetch preserves all 56 KV hashes and removes
   about 811MB from the observed peak, while post-output-release commit stays near 4.08GB.
   This isolates the output-copy contribution from native computation/retention.
3. **Native session high-water survives output collection.** Baseline JS external memory
   returns from about 1.116GB to 10MB after outputs are dropped, but Private Bytes stays
   at 4.084GB. Session disposal then drops it to 277MB. This reproduces high-water retention
   with one prefill; neither multi-turn replay storage nor a growing JS transcript is required.
4. **The q4 graph has a large dense component.** Read-only ONNX metadata inspection of the
   pinned 919096585-byte file finds `model.embed_tokens.weight`, float32 `[151936,1024]`,
   **622329856 bytes**. Its transpose feeds the ordinary MatMul LM head. Thus q4 does not
   mean all weights/output paths are int4. The exact native share of transpose/packing and
   arena reservations is not independently measured; no additional 622MB resident copy is
   asserted solely from the graph.

Primary references: [ORT v1.24.3 CPU output bridge](https://github.com/microsoft/onnxruntime/blob/v1.24.3/js/node/src/tensor_helper.cc#L281-L295),
[versioned ONNX format](https://github.com/onnx/onnx/blob/v1.19.1/onnx/onnx.proto),
[ORT session-option parsing](https://github.com/microsoft/onnxruntime/blob/v1.24.3/js/node/src/session_options_helper.cc#L176-L188).
Observed tensor sizes/weights above come from the local pinned graph and native profile,
not estimates extrapolated from model parameter count.

## Rejected shortcuts and next bounded step

Disabling prepacking worsens commit and changes some output hashes; it is rejected. Omitting
logits fetches is diagnostic only: the answer generator needs logits, and native allocation
still happens. No such change is promoted. Previous arena-off timing/2 GiB failure remains.

The next minimal candidate should compute **only the last hidden position at the LM head**,
while keeping the complete prompt/attention/KV and full output allowance. Slicing after
the existing native output arrives cannot remove that allocation/copy. This requires a
separately identified graph/export candidate; the pinned graph cannot enable it through a
missing runtime input. Verify full generation tokens, native publication/replay/cancel and
resource limits before promotion. This report does not claim the export alone will pass 2 GiB.

Do not treat `RunOptions.extra.memory.enable_memory_arena_shrinkage` as active in this Node
binding: its [v1.24.3 run-option parser](https://github.com/microsoft/onnxruntime/blob/v1.24.3/js/node/src/run_options_helper.cc)
only reads logging severity and tag. Session extras and native profiling are implemented,
despite stale placeholder comments in parts of the TypeScript declarations.

## Validation and evidence

Actual model-stage runs, baseline output-hash/first-token comparison, Node typecheck/build,
and the three real Windows monitor-control tests pass. Strict mypy source/admission probes
passes 50 files. Core/native publication code is unchanged; no full unrelated suite is run.

## Standards

Fresh read-only review found no documented-standard breach. The small repeated tensor
metadata mapping is an optional duplication concern; no extra abstraction is introduced.

## Spec

Fresh read-only review found one P2 ownership-control defect, reproduced against the actual
baseline-02 receipt, then fixed with explicit GC rooting and the held-byte assertion. Narrow
re-review passed; the final real rooted run confirms the held/released buffer transitions.
Remaining native allocation/long-varying-input and 2 GiB acceptance is still open.

External receipts under `Documents/Codex/2026-11-04`: `evernook-86-prefill-{baseline,no-prepack,no-logits}-01`,
matching monitor directories, baseline-02 consistency receipts, `evernook-86-prefill-equivalence-01.json`,
`evernook-86-prefill-phase-comparison-01.json`, native profile summaries, and the read-only
graph metadata/script. All use captured synthetic data; no raw feed, asset or runtime is
committed. Earlier variant receipts precede the extra first-token/session-options fields;
their source/version difference is not hidden or relabeled as the final probe.
Rooted verification and `evernook-86-prefill-rooted-check.json` are under `2026-11-05`.
