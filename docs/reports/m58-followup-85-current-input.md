# #85 — current input is not historical evidence

The dsh/pi adapters pass the current capture receipt as `current_input_id` to Core/MCP
context and recall. SQL excludes that one ID before exact/lexical pagination, recent-window
coverage and semantic scan limits. Context also omits a pinned/derived item depending on
that source. Original capture, native prompt, prior identical text, source visibility,
version checks, token budgets and durable publication keep their existing contracts.
The optional scope lasts one request; it is neither text deduplication nor persistent suppression.

Public Core reproduced the missing scope as an unexpected argument. Real SDK regression
reproduced current-ID self-citation in actual provider evidence, in both legacy adapters and
budgeted dsh; budgeted selection can already omit the current item accidentally, so the
regression also checks the first turn with no historical candidates. After repair all four
dsh/pi legacy/budgeted cases pass (13.811 seconds), using independent public MCP readback.
They confirm current text remains native/saved, old identical text remains historical,
and a following Core turn can recall the former current receipt. Semantic-only recall is
also checked with built indexes and limit 1, and unbuilt indexes do not return a false self-hit.

Additional public Core/context/semantic regression: 26/26 in 27.107 seconds before the
additional semantic-only case. MCP scope test passes in 2.703 seconds. Strict mypy covers
94 files; TypeScript checking/build passes. Eight existing native regressions cover target
token budgets, protected input after restart, cancellation during construction/durable
publication, independent replay, suppression/correction/purge: 8/8 in 42.164 seconds within
a 120-second owned-tree wrapper. All test data is synthetic, with a separate external
editable environment bound to this checkout rather than the old installed candidate.

These tests establish a data-path repair, not improved model-answer quality. The original
224-question strict score and temporal/format failures remain recorded; no new normalization,
gold-driven payload or profile activation is introduced. A bounded fixed-model diagnostic
is tracked separately with #86; full before/after quality admission remains incomplete.

Two-axis review found a derived-profile edge: excluding a current-dependent tag only at
the final snapshot still let it consume the profile allowance. A two-tag public regression
reproduced the historical tag disappearing (0.843 seconds); exclusion now happens before
profile charging, and the historical tag survives (0.721 seconds). The final source-snapshot
filter remains as defense. Standards review found no other required changes.
