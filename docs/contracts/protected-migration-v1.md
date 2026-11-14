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
