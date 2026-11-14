# Encrypted Vault Backup Implementation Plan

**Goal:** Produce an authenticated, independently recoverable encrypted package for a single fully linked protected inbox without exposing private content or passwords.

**Architecture:** A trusted administrator requests backup through the service, which owns the inbox and obtains its exclusive ownership gate and both SQLite writer locks. Reuse existing inbox signature, projection and ledger-link validation; separately fingerprint immutable links so later ordinary visibility controls can remain authoritative. Use the existing sealed-recipient, authenticated secretstream and signing primitives for the inbox snapshot and key materials. The backup recovery key and vault password remain distinct. Restoring and verified reattachment are separate dependent work, not implied by package creation.

**Tech Stack:** Python 3.14, SQLite, PyNaCl 1.6.2, existing local authenticated service and unittest.

**Spec:** #213 and `docs/superpowers/specs/2026-11-14-offline-backup-recovery-design.md`.

## Constraints

- No plaintext, vault password or backup recovery password reaches service requests, logs, returned receipts, committed fixtures or the encrypted package.
- Only a configured, single, registered inbox whose every object and protected link are complete and consistent qualifies. Unknown inboxes cannot be discovered; no silent omission.
- Backup runs under bounded exclusive maintenance, uses no network and never deletes or mutates the source; preserve existing capture, privacy, cutover and admission boundaries.
- Validate signatures, sender keys and binding; do not claim that creating a package proves a password can unlock every secret. A later trusted restore must verify this separately.
- New destination only; atomic ciphertext publication; private temporary files can remain after forced process termination and must be documented.
- Snapshot and subsequent source changes are distinct. Never replace current suppression or correction state with backup-time state.

## Steps

1. Add synthetic contract tests for encrypted package, exact ledger/link binding, missing/pending or tampered source, authorization and original source preservation.
2. Implement bounded service-owned snapshot and encryption, plus trusted Python SDK/CLI; reject path overlap and missing dependency without plaintext fallback.
3. Test real service, exclusive gate/concurrent writes, cancellation and before/after-publication crash behavior; test empty inbox and changed controls.
4. Verify affected source and installed-wheel tests, typing and output privacy. Document exact limitations; commit and push one verified ticket and update #213/#117.
