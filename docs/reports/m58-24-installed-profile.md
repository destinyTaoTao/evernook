# M58-24 — restricted installed profile

Ticket: [#82](https://github.com/destinyTaoTao/evernook/issues/82), part of Spec #58.

Status: isolated installed checks and the frozen long-run window completed; **restricted, no
daily model activation**. Final merged-source reconciliation passed. The dependent #81
campaign remains blocked by failed quality/resource/latency gates and missing scale observations.
The [frozen policy](../../probes/installed/policy.json) SHA-256 is
`b8ba4d886ec90128f66feba004800b8ba515cb089ab869e42c9a15a5fce59331`.
The [reproduction/recovery guide](../../probes/installed/README.md) states exact boundaries.

## Confirmed repair

An actual installed 0.1.0 wheel reproduced revoked processor admission returning through a
verified old archive: retaining enabled/tier/categories while setting admitted_processor to
null left the control head unchanged, and runtime publication did not compare that settings
table. The restored runtime therefore contained the previous admitted rules processor.

Two public API regressions failed before the repair. Core 0.1.1 now includes
`preference_updates_settings_v1` in the authenticated control head and publication watermarks.
Absent optional tables preserve old control identities. A current checkpoint cannot make an
old snapshot's revoked admission safe: publication rejects it until a fresh current snapshot
is exported/replayed. The original-data, authority and storage-v5 contracts are otherwise
unchanged. Upgrades require stopping old binaries and recapturing affected checkpoints.

## Artifacts and actual checks

The candidate external source snapshot combines the frozen #81 Core bytes with the two #82
repair modules and 0.1.1 metadata. It was built before merge; final main's 39 Core files and
28 npm members now match the frozen wheel, package and external installation byte for byte.

- Core wheel 0.1.1 SHA-256:
  `722cfae9da493483e5d8a8ad0c42b6d1f8383ca1db440175d5112eb017be1555`.
- Local npm package 0.1.0 SHA-256:
  `38c34dd80fcc6ff168631ce992c891057c8443a988802668e71a7fbd8ea6d2bc`.
  Package contents are limited to dist, README and metadata; this is not registry publication.
- Source restore file: 11/11 pass, 72.744 seconds. Installed wheel restore file:
  11/11 pass, 71.672 seconds. Both include the new revocation regressions.
- Bare installed 0.1.0 preflight: 9 checks pass without MCP/PyNaCl, preserving ordinary text,
  important text, idempotency and sensitive projection while vault is off.
- Installed 0.1.1 lifecycle: 22 checks pass, including explicit current correction/prior knowledge,
  suppression, confirmed ordinary-source purge, no resurrection, exact reliable stream
  and independent-directory recovery. This is not automatic extraction or physical disk erasure.
- Actual 0.1.0-created synthetic data opened through separately installed 0.1.1 retains exact
  original, source identity and idempotent receipt. No launcher is changed. A tampered-wheel
  preflight returns exit 2 before profile open; the synthetic database digest is unchanged.
- Actual installed dsh and pi SDK/adapter checks pass: known source evidence ID reaches the
  deterministic provider, first/final visible stream independently replays and network attempts
  are zero. Imports resolve from the external installation. Legacy context and synthetic provider
  do not establish target-token, real-model or desktop-installed acceptance.

## Long-run status and limitations

The formal window is 1800 seconds with one foreground iteration every 10 seconds, actual
concurrent navigation/index work, least-privilege identities, acknowledged stream readback,
cancellation, owned-service exit and client restart. Resource sampling is 250 ms, observed
owned-tree Private Bytes threshold 2 GiB and experiment disk threshold 10 GiB. Other explicitly
recorded #81 experiments share the machine; no globally idle system claim is made.

Pilot-01 passed outside the resource wrapper. Formal attempt-02 failed its initial MCP
connection before any foreground iteration and is excluded. The probe then waited for its
maintenance window before emitting the failure; startup/progress reporting and a maintenance
stop signal were added. Initialization now starts foreground connections before maintenance,
matching native adapter order. The connection failure's underlying cause is unproven; it is
not described as a repaired production defect. Pilot-03 under the same hidden resource wrapper
passed, taking 37.654 seconds with 150 resource samples, query failures zero, observed peak
390,213,632 bytes, zero network attempts and verified cancel/service-exit/client-restart paths.
An immediate post-run process query found no MCP service matching its unique fixture path.

Fresh formal attempt-04 completed 180 foreground rounds with zero recorded errors and zero
network attempts. Driver elapsed 1807.429 seconds; resource wrapper elapsed 1808.148 seconds,
7216 samples, zero resource-query failures, observed owned-tree peak **391,532,544 bytes**
(below the frozen 2 GiB sampled threshold). Cancellation, acknowledged content after owned
service exit and fresh-client stream replay all passed. The last maintenance receipt has
542/542 messages indexed, navigation cursor/head 542/542 and queued/running/failed all zero.
This is concurrent navigation/index work, not a claim that all generic model jobs were consumed.

The final logical disk snapshot charged all external m82 prefixes plus the isolated worktree,
including installed Node dependencies: **527,943,519 bytes**, below 10 GiB. It counts hardlinks
again and excludes shared global interpreter/uv/npm caches. It is a final logical-size snapshot,
not peak physical disk allocation. The long-run driver SHA-256 is
`825c1a98b8d413ed2363a6adf1a063eceba4676417259397c1d78be2d2ec8b7c`;
maintenance helper SHA-256 is
`15fcf8909c1a1ee3ef98a1331026c0e32e200139aabe659a6513e35bb5dc3140`.
The final receipts establish this isolated window only. Sampled peak is not a hard peak bound and may
miss short-lived children or post-root orphans.

Physical sleep, OS reboot, whole-machine disconnection, desktop UI, unattended daily model
operation and automatic binary/data downgrade remain **unmeasured**. Installed native smoke is
synthetic provider/legacy context. #81 model/scale/quality gates are not cleared by this ticket.
No user assets or existing daily profile were switched.

## Review follow-through and final code checks

The installed navigation backlog probe paused work, accumulated 192 originals, resumed in a
separate installed process with bounded 16-message/one-job ticks, and converged while ten
foreground writes/readbacks overlapped actual maintenance intervals. All seven checks passed.
This covers navigation backlog; generic derived/model queues are deliberately not declared drained.

Normal native-prefix proof is eventual independent readback. A separate installed-chain fault
probe holds a real SQLite writer lock after provider dispatch, independently observes public
capture fail with `storage_busy`, and requires zero native-visible text. Both dsh and pi passed
that check. The first diagnostic incorrectly expected an unavailable error from read-only
status (audit is best effort); those failed diagnostic receipts are preserved, not counted.

An actual isolated test launcher selected installed 0.1.0 with compatible pre-upgrade data,
then installed 0.1.1 with an independently published candidate copy, then explicitly returned
to the previous installed binary/data pair. All seven checks passed, including exact original
and important text and retained inactive candidate-only data. Candidate additions are not in
the active pre-upgrade copy. This is not lossless rollback, a user/desktop launcher switch,
an unsafe downgrade test or verification of a mechanism that refuses unsafe downgrade.

Full source Python **409/409** passed (1433.414 seconds). SDK **85/85** using the installed
candidate Core passed (708.932 seconds), no skips/cancellations. Strict mypy passed source,
tests and installed probes (99 files); TypeScript typecheck/build passed. Final merged main
passed the same byte comparison for all **39** Core package files and **28** npm members.
Post-merge targeted tests passed: semantic 14/14 (19.211 seconds), runtime restore 11/11
(49.768 seconds). Combined strict mypy passed 109 files including both experiment suites;
Node typecheck/build passed. The main editable environment now reports 0.1.1 and resolves
the main source. These targeted checks do not claim a new full combined-suite run.

### Standards

Read-only review found no confirmed standards violations or material code smells. It required
the final window/resource/disk receipts and source identity reconciliation to remain explicit.

### Spec

Review found missing backlog, insufficient native publication-order fault coverage and an
unexecuted rollback procedure. The dedicated backlog, real failed-commit and isolated
version/data-pair rollback probes above address those coverage gaps. No additional production
defect was found. Physical OS/UI and #81 admission limits remain partial acceptance boundaries.

External receipts are under
the external evidence root's `evernook-m82-*` directories. The candidate's
`reconciliation-postmerge-01.json` records final file comparisons; the delivery receipt
records the final Git identity separately. Unmeasured OS boundaries and #81 failed admission
remain required before any broader completion or daily-use claim.
