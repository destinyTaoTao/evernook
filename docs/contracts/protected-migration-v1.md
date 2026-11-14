# Verified single-inbox service cutover

Core 0.1.70 provides an explicit local path for migrating one **declared,
registered, fully linked** protected inbox into the independent service. It
does not automatically discover other inboxes, migrate multiple inboxes,
recover missing keys, provide a backup, or establish physical owner identity.
The existing default still refuses activation when protected objects exist.

Before using it, stop every legacy host and writer, inventory all inboxes and
their reverse ledger links, and reconcile every pending object using the
original authorized entry. Unknown or never-registered inboxes cannot be
detected from the ledger. A legacy program that ignores the service mode and
ownership gate can write again after the cutover lock is released; stopping it
is an operator prerequisite, not something this implementation can prove.

Core 0.1.84 adds `vault-migration-inventory` and the trusted Python
`vault_migration.inventory_entries` page for the declared inbox. An admin sees
only each verified object's ID, state and source entry; use `--after` until
`next_cursor` is null. This metadata is visible to the administrator, so entry
names must not contain secrets. The page is advisory, does not decrypt or grant
cutover, and concurrent changes across pages are not a consistent snapshot.

With writers stopped, a concrete single-inbox sequence is:

1. Use `vault-known` and the operator's own records to identify all inboxes;
   explicitly `vault-register` the chosen inbox if needed. Unknown unregistered
   inboxes cannot be found automatically. Extra registered inboxes block cutover.
2. Run `vault-migration-inventory --config <runtime.json> --access <admin-access.json> --inbox-dir <inbox-dir> --key-dir <key-dir>`
   through every page. For each `protected_pending_link`, use the **same source
   entry's** paired capture access with `vault-reconcile --object-id <id>` and
   the same config/inbox/key options. The admin identity does not acquire other
   entries' rights. Each source entry also needs a paired recall-scoped identity
   for the final preparation. Re-list after an interruption; successful reconciliations
   are idempotent, while a lost response is not evidence of failure.
3. Use interactive `vault-verify` with administrator access and the vault
   password through every page; this verifies decryption without returning the
   originals and is not an activation proof. The final preparation below repeats
   whole-inbox verification under the exclusive gate.
4. Explicitly prepare and activate as shown below. A service started without
   verified activation while service mode is still off does not establish a
   single authoritative writer; see #150. Verify the reported service state,
   and retain original materials and a separate protected backup.

This sequence supports one declared, registered inbox with multiple entries. It
does not yet migrate multiple inboxes together or discover undeclared copies;
it does not establish physical owner identity, real lock-screen behavior, or
daily-use admission.

For several known legacy inboxes, `vault-verify-set` accepts 2–16 repeated
`--inbox-dir` and `--key-dir` options in matching order, together with `--config`
and an administrator `--access`. It prompts locally for each inbox password;
passwords must not be supplied in arguments or redirected input. The trusted
Python equivalent is `vault_multi_verify.verify_declared`. The declared IDs and
keys must exactly match the ledger registry. Under the exclusive ownership gate
and inbox transactions, preflight unlocks and checks every object, both pending
crash windows, source uniqueness and all ledger links. It reports only IDs and
counts and creates no cutover proof. Every declared inbox, key directory and
the ledger directory must be distinct and non-nested; older layouts that nest
these directories are refused and require a separate safe relocation procedure.
It does not change protected objects,
links or service mode; permission checks may write or prune ordinary audit
records and SQLite may create journal files. The 10,000-object and 120-second
checks are cooperative: blocking system calls or one password derivation may
exceed the deadline. Unknown unregistered inboxes are not discoverable, and a
successful preflight is not a backup, multi-inbox cutover or recovery proof.

Core 0.1.88 adds `vault-prepare-set` using the same repeated path arguments.
It requires a real local console, an explicit `STOPPED_AND_INVENTORIED`
acknowledgment and each vault's hidden password. A preparation attempt first
invalidates its earlier multi-inbox proof; all pending objects must be reconciled.
While holding the ledger and all declared inbox locks, it decrypts and checks
all originals and stores a ledger-key-authenticated proof for at most ten
minutes. The proof binds registry IDs, paths, key material, clients, every
linked encrypted object and its ledger reference, and visibility state; it
contains no password or original plaintext. It also checks that the on-disk
ledger fingerprint key matches the key already loaded by the process, before
and after verification. Validity depends on the system UTC
clock and the stated cooperative I/O limits. `vault-check-set` and trusted
Python `vault_multi_proof.revalidate` recheck the snapshot without passwords,
but their return is advisory after the locks are released: it **does not**
authorize activation. Any future multi-inbox activation must recheck inside
its own cutover locks. A crash after proof publication may leave a valid proof
despite an unsuccessful response; recheck it instead of assuming either result.
This proof is not a backup, does not discover undeclared inboxes and does not
enable multi-inbox service, recovery, destruction or daily admission.

The #231 increment was checked on synthetic source and non-editable wheel
installations. Tests cover two inboxes, pending refusal, wrong passwords,
changed ciphertext/clients/visibility/key material, tampered or expired proof,
local confirmation cancellation, and an actual child-process exit after proof
publication followed by revalidation. Existing preflight and single-inbox
cutover tests were also rerun. These checks are not a multi-inbox activation or
restoration test; no actual user vault or operating-system lock was exercised.

Core 0.1.89 can activate a fully verified declared multi-inbox set with one
writable primary and explicit historical inboxes. After preparing the proof,
start with `--activate-owner <access> --activate-verified-multi`, the primary
`--protected-inbox <dir> --protected-keys <dir>`, and one paired
`--protected-history-inbox <dir> --protected-history-keys <dir>` per historical
inbox. The service rechecks the proof while holding the ledger and all inbox
locks, writes an authenticated binding and then enables service mode. Later
restarts must provide the same complete set and primary, without the activation
flags. The service validates current signatures, references and key materials;
primary pending writes may be reconciled, but pending historical objects reject
startup. An interruption before service mode is written requires an explicit
retry with a still-valid or newly prepared proof; after mode is written, restart
with the complete set. Process-crash testing does not prove power-loss durability.

The trusted admin `service-protected-inboxes` / `ServiceClient.protected_inboxes`
lists bound IDs only. Inventory, reverse links and single-use local display can
select an inbox ID; omission retains the primary default. Historical originals
are never used for new writes. An exact retry of an existing historical source
returns its original linked receipt; changed content or missing referenced
objects fail before staging. Local display may append audit metadata to a
historical inbox but does not alter its protected objects. Old Core versions
reject the new runtime configuration rather than silently starting with one
inbox. Startup scanning has a cooperative time limit and no large-store
performance guarantee. If the registry still records multiple inboxes, removal
of both the runtime marker and binding fails closed. Simultaneous external
rollback of the runtime configuration, binding and registry or ledger, or
writers that bypass the project's locks, cannot reliably be detected.

**Multi-inbox backup and restore remain unimplemented:** the existing single
vault backup rejects this configuration and an ordinary backup does not include
the vaults. A recovered ordinary backup may enter the existing authenticated
missing-vault mode, allowing ordinary memory while protected access remains
unavailable; it does not reattach any vault. Keep independent verified backups and original materials; #233
tracks complete multi-inbox recovery. This increment does not establish real
OS lock-screen behavior or daily-use admission.

For #229, seven source tests and seven installed-wheel tests passed; after
adding the non-nesting guard, two focused tests passed on each. They cover
different passwords, linked and pending objects, a lost acknowledgment,
wrong/missing keys, omitted or extra inboxes, duplicate sources, tampering,
cooperative writer exclusion and non-interactive CLI rejection. An initial
CLI test exposed Windows `NUL` reporting `isatty() == true`; the new entry now
also requires `GetConsoleMode` on its input handle. Authorization auditing and
the stated limits still apply; these tests did not activate or recover multiple
inboxes, touch user assets or establish daily admission.

The #226 increment was checked with synthetic data: four targeted migration
tests passed on both source and the non-editable 0.1.84 wheel; two additional
source tests covered concurrent old writers and crash after mode persistence.
The multi-entry scenario verified linked and pending objects, per-entry
authorization, wrong passwords, administrative rechecks, restart, decryption,
and the old-writer fence. Strict mypy passed for the changed modules and tests.
No real vault or uncooperative legacy writer was exercised.

On the original device, a trusted administrator runs in an interactive terminal:

```powershell
python -m evernook.cli vault-prepare-migration --config <runtime.json> --access <admin-access.json> --inbox-dir <inbox-dir> --key-dir <key-dir>
```

The operator must type `STOPPED_AND_INVENTORIED` and then enter the vault
password using hidden input. A trusted Python caller of `vault_migration.prepare`
must make the same inventory statement and is responsible for establishing the
operator's intent. The service and MCP accept neither the password nor the
statement. A new preparation invalidates the previous derived proof first,
including when the operator cancels after starting the interactive flow.

Preparation holds the exclusive ownership gate and write transactions on both
the ordinary ledger and the chosen inbox. It requires exactly one matching
registered inbox, no orphan or foreign links, no pending objects, and a paired
identity with recall scope for every source entry. It validates each signature,
decrypts each object, checks its full projection and keyed event fingerprint,
and verifies the corresponding ordinary receipt and protected link. It saves a
10-minute ledger-key-authenticated proof bound to the owner, ledger, paths,
client identities, key materials, object set, links and visibility states.
The proof contains no password or plaintext and does not grant disclosure.
The entry check establishes that a recall-scoped identity is registered; it
does not prove that its separate access file is still available to the user.

Immediately after successful preparation, start the service explicitly:

```powershell
python -m evernook.service --config <runtime.json> --activate-owner <admin-access.json> --protected-inbox <inbox-dir> --protected-keys <key-dir> --activate-verified-inbox
```

The service reauthorizes the administrator and checks the proof and current
state under the same gate and both database write locks before persisting
service mode. It does not decrypt objects again: the authenticated proof binds
the verified ciphertext and recovery material to the rechecked state. A stale,
expired, missing or tampered proof, changed permission or materials, pending
object, mismatched link or additional registered inbox rejects activation.
After activation, restart normally with `--protected-inbox` and
`--protected-keys`, without either activation flag. If a process stops just
after service mode was persisted, normal restart is the recovery path; do not
assume a lost response means cutover did not happen.

The verifier accepts at most 10,000 objects and checks a 120-second deadline
between work units; blocking operating-system I/O or a single operation may
exceed that time. The orphan-projection scan covers the ordinary ledger and
may be costly on a large store; no large-store performance claim is made.
Known key files are checked before and after decryption and again during
activation, but uncooperative same-account file mutation cannot be made atomic
with the database transactions. The operator must keep old processes stopped.
The ordinary backup still omits the protected inbox and recovery materials;
the full #115/#117 privacy, migration, backup and daily-use gates remain open.

Verification for this increment used synthetic data: six targeted source tests,
15 existing service cutover tests, and eight tests against the installed 0.1.70
wheel passed. The installed tests covered a complete cutover, rejection of
pending or changed data, authorization and proof expiry, concurrent inbox
writes, and recovery after a process crash. Strict mypy passed for the changed
Python modules and migration tests. The interactive acknowledgement was tested
with a simulated terminal; no real user vault, physical device lock, backup
restoration, or unknown legacy inbox was exercised.
