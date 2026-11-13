# Restricted installed-profile verification

These probes use synthetic external profiles and actual local wheel/npm artifacts. They do
not switch a user's launcher, overwrite existing assets or activate a model. Failed/missing
#81 admission gates keep the profile restricted. Physical Windows sleep, reboot, whole-machine
network disconnection and desktop UI operation are separate, unmeasured boundaries.

The frozen [policy](policy.json) sets a 1800-second observation window, 10-second foreground
interval, 250-ms resource sampling, 2 GiB observed owned-process limit and 10 GiB experiment
disk limit. Core minimum is 0.1.1; Python 3.14.4, Node 24.12.0, dsh 0.2.1-rc.3 and pi 1.4.0
are the exercised versions. An artifact hash/source manifest is required in addition to a
version string. Do not infer that arbitrary earlier builds are compatible or safe.

## Build and install

Build Core with `uv build --wheel --offline --out-dir EXTERNAL\wheel`. Build the Node package
with `npm ci --offline --ignore-scripts`, `npm run build`, then `npm pack --pack-destination
EXTERNAL\node --json`. Inspect the pack receipt: only `dist`, README and package metadata
belong in the package. These commands make local artifacts, not a registry publication.

Create a new external environment with the fixed Python interpreter. Install the verified
wheel using `uv pip install --python EXTERNAL\env\Scripts\python.exe --offline WHEEL[mcp]`.
Use a separate bare environment with the plain wheel to verify operation without optional
MCP/PyNaCl. Build a separate external Node host from the existing dependency lock, changing
only its root package name and removing the source package's exports/files metadata. Run
`npm ci --offline --ignore-scripts`, then install the verified local tgz with
`npm install --offline --ignore-scripts --no-save TGZ`. Do not reuse a user's host directory.

Check SHA-256 against a separately retained trusted receipt **before** installing. A digest
computed from the same untrusted artifact is not an integrity reference. Retain the old
environment and package receipts; avoid in-place upgrades or concurrent versions on one
runtime. The exercised package snapshot combines the #81 vector-count source with the #82
restore fix; reconcile its per-file hashes with the final merged source before delivery.

## Public installed Core checks

`path_race.py --output EXTERNAL\fresh-path-race --iterations 100` exercises actual Runtime
startup alongside an owned SQLite writer that repeatedly commits and closes connections.
Run it with the intended installed Python from outside the checkout. It records the installed
version/import path, actual writes and startup failures; it checks writer liveness and stops
within its bounded run. This targets the Core 0.1.2 Windows optional-journal/path/ACL fix;
it does not replace the frozen installed-profile stability window.

Clear `PYTHONPATH` and use the installed interpreter from an external working directory:

```powershell
E:\lab\env\Scripts\python.exe probes/installed/lifecycle.py --output E:\lab\lifecycle-01
E:\lab\env\Scripts\python.exe probes/installed/fixture.py prepare --output E:\lab\longrun-01
```

Use absolute probe paths when running outside the source checkout. `lifecycle.py` verifies
exact originals/important text, idempotency, explicit corrections and earlier knowledge,
suppression, confirmed purge/no resurrection, reliable text and independent runtime restore.
It also verifies that an old admitted snapshot cannot restore a revoked processor. Facts are
explicitly seeded, not automatically extracted. The fixture creates separate capture/recall
and maintenance identities; its trusted owner credential remains inside the private runtime.
All records are synthetic, and the optional vault stays off.

`backlog.py exercise --output NEW_EXTERNAL_DIR` pauses navigation, accumulates 192 actual
originals, starts a separate installed process and resumes bounded 16-message/one-job ticks.
It requires cursor/head and queued/running convergence, multiple ticks, exact foreground reads
and overlapping foreground/maintenance intervals. It verifies navigation backlog only, not
completion of every generic derived/model job.

`upgrade.py` reads the known synthetic preflight profile with the named separately installed
version and records import path, exact text, stable source ID and receipt. Run once with the
old environment and once with the candidate plus `--previous OLD_REPORT`. This checks existing
data compatibility without changing any launcher. Optional `--artifact`/`--expected-sha256`
rejects a mismatch before opening the profile. This is an integrity preflight, not an installer
transaction or a binary downgrade test.

## Installed Node/MCP and native SDK checks

From `plugins/harnesses`, using the installed host root and interpreter:

```powershell
node probes/installed.ts E:\lab\node-host E:\lab\env\Scripts\python.exe E:\lab\longrun-01 E:\repo\probes\installed\policy.json
node probes/installed-native.ts E:\lab\node-host E:\lab\env\Scripts\python.exe E:\lab\longrun-01 dsh
node probes/installed-native.ts E:\lab\node-host E:\lab\env\Scripts\python.exe E:\lab\longrun-01 pi
```

The long-run driver loads `@evernook/harnesses/client` from the actual installed host, starts
the installed Python MCP service, checks least privilege/vault-off and then starts concurrent
navigation/lexical maintenance. Foreground startup precedes background work, matching native
adapter initialization. It records originals and independent first/final stream replay, cancel
state, acknowledged data after forcibly ending its own live public transport PID, and a fresh
client restart. It does not simulate a physical power failure or claim native adapter recovery
from every transport failure. A stop file releases owned maintenance on failure; incomplete
windows and failed startup are preserved. A final positional `30` is a pilot, never the formal
1800-second window. Always prepare a fresh fixture after a pilot/failure.

Wrap the command with `probes/admission/monitor.py` from #81 to measure the owned live process
tree at 250 ms. Its sampled peak is a lower-bound observation, not a hard maximum guarantee;
short-lived children and post-root orphans may be missed. The driver writes startup/progress
immediately so an unsuccessful bootstrap cannot masquerade as a quiet successful long run.
Worker-level network denial is not an OS firewall/disconnection test.

The native driver resolves imports with `import.meta.resolve` **inside the installed host**,
including packages whose exports allow ESM only. It uses actual installed dsh/pi SDKs and
adapters with a deterministic local provider, no real model or endpoint. The provider records
whether it received the known evidence ID; independently read visible text must equal the
published stream. Context is legacy mode, so this cannot satisfy real target-token/model
admission. It is an installation/lifecycle check, not a quality benchmark or desktop UI test.
An optional trial label creates a distinct independent test session/output directory; it must
not be used to bypass an uncertain source conflict.

Normal prefix checks establish eventual independent readback. For the separate failed-commit
case use a fresh fixture and append `TRIAL lock-first-commit`. After provider dispatch the
probe holds a real SQLite writer lock, independently confirms public capture fails with
`storage_busy`, and requires zero native-visible text. The lock helper accepts only the
explicit synthetic fixture and releases on stdin EOF. Do not run this fault against a user's
runtime or the active long-run fixture.

## Upgrade and recovery operations

Stop old hosts and maintenance before upgrading. Export current ordinary evidence, retain an
authenticated control checkpoint separately and install into a new environment. Validate the
candidate against an isolated profile before switching an explicitly selected authoritative
runtime. Current storage remains v5; that does not make mixed old/new binaries safe.

Core 0.1.1 binds processor admission to the control checkpoint and publication watermarks.
If automatic settings have been used, recapture old checkpoints and export a fresh current
snapshot before controlled runtime publication. `archive_checkpoint_stale` or
`archive_evidence_incomplete` requires reconciliation/re-export, not ignoring the control.
`source_conflict` requires comparing existing stable evidence, not renaming IDs blindly.
`source_purged` must remain blocked; restore cannot recreate the original.

If candidate validation or artifact integrity fails, keep the existing launcher untouched and
retain the failed output. Re-select the previous verified environment only with a compatible,
pre-upgrade isolated data copy and explicit operator selection; never let 0.1.0 process a
runtime whose admission was revoked under 0.1.1.

`rollback.py prepare` in the separate installed 0.1.0 environment, then `rollback.py exercise`
in installed 0.1.1, executes a synthetic launcher/data-pair selection test. It publishes an
independent candidate copy, selects the installed candidate, writes a candidate-only record,
then explicitly selects the previous installed binary with its compatible pre-upgrade data.
Original and important text must survive, and candidate-only additions remain in the retained
inactive candidate copy. This is not a lossless rollback of newly used data or an official
desktop-launcher test. Automatic downgrade, disaster recovery without trusted materials and
switching the user's profile have not been exercised. Prefer the current fixed version's guarded independent
runtime recovery path. Recovered and original runtimes do not synchronize with each other.
