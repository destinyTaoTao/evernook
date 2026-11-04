# #83 — one tokenizer worker per context construction

`HostContext.build` owns a bounded `LocalTokenCounter.session`; one lazily started local
Node worker handles its counts and is reaped before return. Standalone `count` retains its
one-shot EOF protocol. There is no cross-build service or token-count cache. Every request
rehashes both pinned assets; only the verified tokenizer object is reused. Sequenced responses
reject stale counts. Private text is never emitted on worker failure.

The session has the construction's positive timeout (maximum 120 seconds), individual counts
retain the 10-second bound, cancellation is polled and an overall timer also stops the owned
Node process. Pipe I/O runs on one owned exchange thread; cleanup stops the worker and closes
the exchange. The worker's input EOF also handles abrupt owner exit. Configured Node and
tokenizer code remain trusted local dependencies, not a hostile same-account sandbox.

Baseline source `13c3545` versus repaired source used the same pinned real Qwen tokenizer,
24 frozen synthetic trace payloads and a separate six-original/six-pin context fixture. Token
budget remained 4096. Observations on the same recorded Windows machine:

| Measurement | Baseline | Scoped worker |
| --- | ---: | ---: |
| First count ms | 314.035 | 320.460 |
| 24 counts total ms | 7508.427 | 613.145 |
| One complete context construction ms | 2614.474 | 634.132 |
| Observed owned-tree peak bytes | 183144448 | 294051840 |

All 24 counts, final payload bytes, counter identity and used tokens (1904) match exactly.
The independent existing TypeScript host counter also counts that payload as 1904. The
additional 23 scoped counts took 10.643–15.261 ms each. Process reuse trades bounded live
memory for avoided startup; the observed peak increased and remains below the original
2 GiB threshold. This is a small before/after diagnostic, not formal p95 or overall admission.
The wrapper caps each run at 40 seconds; actual windows were 11.025/2.013 seconds.

Validation: real target-tokenizer, in-session tampering, nonresponsive-worker timeout,
cancellation, abrupt owner exit and host-build cleanup **8/8** pass (3.752 seconds).
Strict mypy source/tests passes 94 files. Inputs/outputs and resource receipts are external
under `evernook-followup-83-small-01`; `native-counter.json` is the independent host check.
Full provider/SDK regression and formal quality/scale admission remain separate checks;
no sensitive/source guard, context selection or default model profile is relaxed.
