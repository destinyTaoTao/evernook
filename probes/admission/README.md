# M58-23 admission experiments

These probes collect evidence; they never activate a daily profile or admit a model.
All runtime data, models, credentials and experiment outputs must be outside the checkout.
Use the repository Python 3.14 environment and the locked harness dependencies with Node 24.12.0.

`policy.json` was frozen before the experiments (SHA-256
`6b39e961a91fb1311fa498b2db80a61f1cfa64bb62813f31c8ef9f07e901a4ba`).
It fixes the models, suite/scoring identity, hardware, quality, latency, resources,
sample counts and coverage requirements. A failed threshold remains failed.
The suite uses maintainer-delegated AI review; it is not human-reviewed gold.

## Quality

From the repository root, run each phase into a **new** external directory:

```powershell
.venv\Scripts\python.exe probes/admission/quality.py core --output E:\lab\core-01
.venv\Scripts\python.exe probes/admission/quality.py automatic --output E:\lab\auto-01
.venv\Scripts\python.exe probes/admission/quality.py prepare --entry dsh --embedding-config E:\models\embedding-profile.json --output E:\lab\dsh-fixtures-01
```

`prepare --entry pi` prepares a separate fixture for pi. Each question has an independent
runtime containing only its permitted source originals. Facts are explicitly seeded through
public APIs only when the complete permitted fact timeline is available. Partial contexts
remain raw evidence; missing predecessor facts are not invented. This does not test automatic
fact extraction. Native prompts receive the original question, without gold answers, aliases
or extra fixture-only temporal controls. Unsupported native time controls remain failures.

From `plugins/harnesses`, the actual pinned SDK driver is:

```powershell
node probes/admission.ts dsh E:\lab\dsh-fixtures-01\host-plan.json E:\lab\dsh-quality-01 E:\models\qwen-profile.json E:\models\embedding-profile.json C:\myProject\evernook\.venv\Scripts\python.exe
```

Use `pi` for the other SDK. Optional final positional `1` runs a pilot. A used fixture
contains the native prompt/output and must not be reused for another independent trial.
Create fresh fixtures after a failed pilot, rather than changing source IDs to bypass a conflict.
Outputs contain actual native-visible text, provider requests and raw local model diagnostics.
Only visible text is scored. UUID-to-corpus source normalization does not repair model values,
invalid JSON, refusals, missing results or unsupported time controls.

`quality_report.py --core DIR --automatic DIR --host DIR --output NEW_DIR` merges one host's
observations and invokes the frozen public scorer. Run it separately for dsh and pi.
The automatic collector explicitly enables only the existing rules processor in isolated
fixtures. Its conservative `review` fallback is a collector mapping, not a model classification.
No accepted records means automatic acceptance precision is undefined.

## Scale

```powershell
.venv\Scripts\python.exe probes/admission/scale_ingest.py --messages 100000 --output E:\lab\scale-100k-01
.venv\Scripts\python.exe probes/admission/scale_index.py --fixture E:\lab\scale-100k-01 --embedding-config E:\models\embedding-profile.json --output E:\lab\index-01
```

Repeat with `--messages 1000000`. Ingestion uses public submit/read/status and stores every
submission latency in `submit-ms.f64le` (little-endian doubles). A time/resource stop preserves
the actual count and failure; it is not a complete million-message fixture. Indexing uses the
real pinned MiniLM backend, first targeting 10016 stored fragments for the scan-limit probe,
then lexical maintenance within one frozen index budget. `stored_vectors` counts actual stored
rows for the configured space, including stale rows. It is not inferred from message counts
and does not imply complete/current coverage. Partial coverage blocks admission.

After a writer has stopped, `scale_receipt.py --fixture DIR --embedding-config PROFILE
--output NEW_EXTERNAL_DIR` reconciles public counts, actual stored-vector status and selected
exact source roundtrips. An incomplete target exits 2 and cannot authorize a target-size query
matrix. Ingestion's native memory counters are sampled at progress checkpoints through the
last submission; they do not include the subsequent sorting/report-construction peak.

`scale_query.py` accepts `--fixture`, `--embedding-config`, `--context-config` and `--output`.
The context config uses the target tokenizer and 4096-token policy. It runs the frozen
recent/historical/no-answer/popular × idle/writing/rebuilding × warm/fresh-process matrix.
The recent target must exist and carry the fixture's recent timestamp. Background progress
and errors are recorded per cell; cells without verified progress are invalid workload samples.
Rebuilding intentionally resets the derived lexical index and maintains real vectors; originals
remain authoritative. Measurements change the synthetic runtime's audit log and background
capture adds synthetic originals; before/after counts are recorded.

`matrix_report.py --matrix DIR --output NEW_EXTERNAL.json` summarizes completed attempt
receipts. Expected, observed, missing and valid-workload counts remain separate. Timings
include all observed calls, including incomplete contexts; they cannot establish the complete
memory latency gate. Active batches without a completed attempt receipt remain missing in a
partial summary. Profiler samples and unscored warmups are excluded. Source-count ranges
show the actual growing runtime population. A missing or failed gate exits 2.

For the separate >10000-fragment diagnostic, use `--cell pure_semantic --result EXTERNAL.json`
instead of `--output`. Record actual scan limits, degradation, target retrieval and incomplete
coverage. Candidates or an empty result cannot prove full historical coverage or absence.

Fresh-process means a new host process, not an OS disk-cache flush. Warm samples reuse the
host process; the existing embedding backend still loads its model per call. Other explicitly
recorded isolated experiments may share the machine, so `idle` means no additional workload
owned by that matrix, not a globally idle workstation.

## Phase and resource diagnostics

`scale_query.py --cell recent --profile --result EXTERNAL.json` also writes a cProfile file and
phase table. Diagnostic profiler overhead is excluded from the formal sample distributions.
Inclusive phases overlap and must not be added. `recall` includes retrieval/fusion; its self
time is fusion/ranking/validation glue, not an isolated RRF-only timer. `embed` includes integrity,
child/model startup and inference. `probes/minilm-startup.ts` reports these model phases in a
separate diagnostic with the same pipeline settings and actual artifact hashes.

The formal matrix also times the public backend and `recall` boundary in the **same call**.
`retrieval_fusion_source_excluding_embedding_ms` is that recall duration minus its nested
embedding duration; it includes retrieval, coverage/status, fusion/ranking, authorization and
source validation. It is not isolated RRF time. No independent sample is subtracted. Warm cells
perform one explicit unscored warmup before the frozen 30 observations.

`plugins/harnesses/probes/ipc.ts` records a transport-only synthetic echo baseline using the
same MCP SDKs, followed by actual memory bootstrap/status/context roundtrips. Echo has no
memory authorization or Core work. It is not subtracted from production samples. Production
roundtrips include IPC, auth/audit and Core work. Model startup, pre-provider memory and first
reliable publication are separate fields in the native quality driver. Each first published
prefix and the final stream are independently read back through another MCP client.

On Windows, wrap a command with:

```powershell
.venv\Scripts\python.exe probes/admission/monitor.py --output E:\lab\monitor-01 --max-seconds 900 -- COMMAND ARGUMENTS
```

The wrapper samples its owned live process tree every 250 ms, records stdout/stderr, and reports
the observed aggregate Private Bytes peak. Short-lived children between samples and post-root
orphans may be missed; this is not a hard peak-memory guarantee. Query failures are explicit.
The wrapper does not kill unrelated processes or change the workstation's network/power state.
Worker-level network blocking does not establish physical OS disconnection or sleep recovery.

The wrapper now defaults to a **900-second overall operator budget**. Create `OUTPUT/STOP`
to stop earlier. Either control attempts to terminate the owned command tree, writes an explicit
stop reason and exits 2; termination errors remain in `stop_error` and require cleanup rather
than implying a stopped tree. Root exit does not prove absence of orphan processes. Partial
or missing samples cannot pass admission. Run `scale_query.py` through
this wrapper: its per-batch timeouts alone do not bound the complete matrix. A deliberately
long formal campaign must explicitly choose a larger operator budget and record it. The
original quality/scale policy and expected sample counts remain unchanged. These controls
were added after the original campaign was stopped; its archived probe bytes and partial
receipts remain separate. `python probes/admission/test_monitor.py -v` verifies deadline,
descendant termination and the stop-file path with real short-lived Windows processes.

Public Core regression for the new vector count is in `tests/contracts/test_semantic.py`.

## One-prefill native memory attribution (#86)

From the checkout root, with an existing external output parent and the same pinned model
profile/captured synthetic provider request:

```powershell
$env:PYTHONPATH = "$PWD\src"
C:\myProject\evernook\.venv\Scripts\python.exe probes/admission/monitor.py --output E:\lab\prefill-monitor-01 --max-seconds 35 -- node --expose-gc plugins/harnesses/probes/prefill-resources.ts E:\models\qwen-profile.json E:\lab\captured-request.json E:\lab\prefill-01 baseline
```

Run `no-prepack` or `no-logits-fetch` in fresh processes/output directories to change one
variable. These are diagnostic interventions, not usable provider settings or admission
samples. The probe captures real prepared feeds before inference, stops that wrapper with
an intentional diagnostic error, then invokes one native prefill directly. Expected capture
diagnostics may appear in stderr. It emits native ORT profiles, output hashes and staged
memory ownership markers. No answer or reliable stream is produced. `monitor.py` adds Unix
observation timestamps to correlate 250ms process samples with the stable phase windows.
See [measured attribution and limitations](../../docs/reports/m58-followup-86-prefill-attribution.md).

## Independent last-hidden LM-head candidate (#86)

Use a separate external Python 3.14 environment containing `onnx==1.20.1`; do not add it to
the Core runtime. With `PYTHONPATH` pointing to checkout `src`, export through
`probes/admission/export_last_head.py --profile ORIGINAL_PROFILE --output NEW_EXTERNAL_ROOT`,
under `monitor.py --max-seconds 60`. Original assets must not overlap the new output.
The exporter pins the original graph, inserts only a final-position Slice before LM head,
checks that node, and proves exact original protobuf restoration after undoing the change.
The original pre-fused graph already fails the standard full-model checker; that identical
parent/candidate limitation is recorded, never called a full checker pass.

The resulting `profile.json` uses `qwen3-0.6b-last-hidden-experimental-v1`. The experimental
loader pins its distinct graph SHA and all unchanged tokenizer/config assets independently;
the original profile still requires the original graph. Original weights, prompt/attention/KV,
32768 window, 4096 memory allowance and 512 output allowance are not reduced. This candidate
is not admitted by the original frozen policy or enabled as the default provider.

`prefill-resources.ts` also writes the final logits row/hash for exact comparison. Run
`model-resources.ts` for the same captured provider request through full generation. For
a one-case real dsh cancellation diagnostic, append `1 cancel-after-first-visible` to
`admission.ts` arguments. It verifies canceled partial replay and delegated model stop;
the observation is explicitly an error/diagnostic, never a successful quality answer.
See [candidate results and remaining gates](../../docs/reports/m58-followup-86-last-head-candidate.md).
These experiment scripts use actual APIs and SDKs for measurement; synthetic or component
success does not establish real daily use, physical power-loss safety or model admission.

For the same-request allocator control, use `prefill-resources.ts ... noarena`, or
`model-resources.ts PROFILE REQUEST NEW_EXTERNAL reuse default|noarena`. The optional
allocator argument defaults to the original behavior. Each invocation records actual session
options and requires its own fresh directory and 35/100-second monitor budget. The prefill
probe also records the model argument kind at the actual ORT creation boundary, without
logging a path string or retaining model buffers for measurement. See the
[arena comparison and rejected hashing hypothesis](../../docs/reports/m58-followup-86-arena-comparison.md).
The candidate remains over the frozen 2 GiB gate; these switches are diagnostic only.

An independent copy of the experimental Qwen profile may select
`"file_access": "verified-local-path-v1"`. It resolves only the already hash-verified q4
graph through the library's custom cache, with scoped/serialized global state and no cache
writes. All nine asset hashes still run before load and every generation. Check startup
`file_access` and `verified_graph_path_hits`; an absent field keeps `library-default`.
`model-file-access-check.ts PROFILE NEW_EXTERNAL original|verified` is the small real-library
8 MiB path-only regression (original fails, verified passes), requiring no model generation.
See [full evidence and limits](../../docs/reports/m58-followup-86-path-cache.md).
This profile remains experimental and over the memory gate; no daily provider is switched.
