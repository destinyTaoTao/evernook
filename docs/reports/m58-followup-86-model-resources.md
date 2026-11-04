# #86 — bounded Qwen process/heap/tensor diagnosis

This is a diagnostic delivery, not a successful 2 GiB repair or model admission. The default
experimental `LocalQwenChat` allocator, pinned model and SDK publication path are unchanged.

## Fixed experiment

Same Windows/i5-13600K machine, Node 24.12.0, pinned Qwen3-0.6B q4 CPU,
Transformers.js 4.2.0 / ORT 1.24.3. The captured synthetic g01-q6 provider request is replayed
twice in each of three separate processes: reuse one session; dispose/rebuild between runs;
reuse with only `enableCpuMemArena: false` changed in an external frozen diagnostic copy.
All nine model artifacts are still rehashed before generation. Prompt is 1321 tokens,
output allowance remains 512, model window 32768, original memory budget 4096. No truncation,
early JSON stop, reduced output cap or scorer change is used. Each command is capped at
100 seconds; actual process windows are 59.106 / 61.610 / 70.627 seconds.

`model-resources.ts` records JS heap/external/ArrayBuffer/RSS phases and observes the public
ORT session output wrappers without keeping tensors. FinalizationRegistry plus explicit
GC is diagnostic instrumentation: wrapper collection does not establish native allocation
release. Instrumented phases/generation times are not production latency or p95 samples.
The separate Windows wrapper samples owned-tree Private Bytes every 250 ms; all three
runs exited normally with zero query failures. Short-lived children and between-sample peaks
may be missed; no hard peak-memory guarantee or whole-OS network isolation is claimed.

| Observation | Reuse | Dispose/rebuild | Arena disabled |
| --- | ---: | ---: | ---: |
| Observed tree Private Bytes peak | 5453262848 | 5864095744 | 4070301696 |
| Pipeline load ms | 2168.734 | 1943.798 / 1966.656 | 1964.373 |
| First generation ms | 27105.916 | 25808.413 | 32438.904 |
| Second generation ms | 25710.083 | 26095.009 | 32230.767 |
| First delta ms, run 1 / 2 | 7049.732 / 6423.893 | 6657.977 / 6617.014 | 7576.274 / 7329.390 |
| RSS after request 1 / 2 | 3292119040 / 3338936320 | 3290009600 / 3325308928 | 1782976512 / 1781321728 |
| RSS after final session disposal | 202784768 | 230916096 | 202518528 |

RSS is physical resident memory, distinct from Private Bytes commit. All six generated
texts and token-ID sequences match exactly (119 forward calls per request). The existing
strict model-answer failure is retained; equivalence here is not answer correctness.
Both repeats emit 6783 ORT output wrappers each; all observed wrappers are finalized by
the completed phases. JS heap stays about 59 MB, external memory about 10 MB. The largest
prefill logits tensor is float32 `[1,1321,151936]`, **802829824 bytes**. Cumulative outputs
over two runs are 77085340672 bytes; that is allocation traffic, not simultaneous live memory.

## Interpretation and decision

The same-input repeat plateaus while session disposal releases most resident memory.
Disabling the arena lowers retained RSS and sampled commit without changing tokens. This
supports substantial native session/allocator high-water retention; it does not prove a JS
reference leak or exclude additional retention across the original 224 varying requests.
The full-sequence logits allocation is directly observed; its causal share of peak commit
and ONNX internal arenas is not separately measured.

Arena disabling still exceeds 2 GiB and costs roughly 6 seconds per request in this diagnostic.
Rebuilding also exceeds the limit and adds startup work. Neither is promoted as the default
fix. No latency/quality requirement is relaxed. The next bounded investigation should focus
on output/intermediate allocation in the pinned graph; changing an export/artifact requires
a separate candidate and fresh admission. No 224-case or scale matrix was restarted.

External receipts: `evernook-followup-86-small-01/{reuse,rebuild,noarena}`, external one-line
allocator variant under `noarena-probe`, and the corresponding three monitor directories.
The frozen variant keeps its exact source; later manifest fields added to the committed
probe are not retroactively claimed as present in earlier baseline receipts. Model loads
record all artifact identities. `comparison.json` records six exact output comparisons.

Two-axis review found a Windows path guard bypass using alternate path casing. The new
CLI now checks physical checkout/parent paths with Windows-aware relative containment;
the output parent must already exist. A public CLI preflight reproduced the old `EEXIST`
instead of the intended refusal, then verified `external_output_required` before data/model
loading after repair. No model generation was rerun for this filesystem-only correction.

## Related #85 real-provider check

A fresh public-API fixture for only g01-q6 ran through real dsh + the original fixed Qwen,
with unchanged scorer and budgets, in a 100-second wrapper (actual 25.065 seconds). Public
`read_source` verifies the newly captured current receipt and exact question. That receipt
is absent from provider evidence and the complete current question remains native. The
first published text independently reads back and final durable replay matches. Input tokens
are 965 versus the original 1321 after removing self-evidence. The answer now cites historical
Java 8 evidence, but the unchanged strict canonical check still fails on sentence formatting.
This is one diagnostic example, not improvement of the full 224-case score; varying receipt
UUIDs and independent fixtures are disclosed. Receipts: `evernook-followup-85-qwen-native-01`,
`scope-check.json`, and `evernook-followup-85-qwen-monitor-01`.
