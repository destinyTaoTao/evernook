# Restored Missing Vault Implementation Plan

**Goal:** Allow explicit ordinary-service activation of an authenticated offline restore while protected assets remain unavailable and intact.

**Architecture:** Verify the key-authenticated offline recovery receipt, current ledger and protected projection consistency under the exclusive ownership gate and administrator authorization. Record a durable missing-vault marker bound to that receipt; every later service start validates the marker and refuses binding a protected inbox until a separate verified restore exists. Expose a nonsecret status. Recover safely if configuration persistence succeeds before the marker transaction commits.

**Tech Stack:** Existing Python service, SQLite, authenticated local transport and unittest.

**Spec:** `docs/superpowers/specs/2026-11-14-offline-backup-recovery-design.md`; #212.

## Constraints and review focus

- Only offline-recovered receipts explicitly declaring missing protected assets qualify; normal activation remains fail-closed.
- Administrator authorization is rechecked inside the gate and the database transaction; legacy writers must already be stopped.
- No vault object or key is supplied, copied, deleted, decrypted or made writable by this mode.
- A missing, modified or inconsistent marker/receipt and protected orphans fail closed, including on restart and after a crash.
- Restore of actual protected assets remains a separate feature; no quality or daily-use admission is granted.

## Tasks

1. Add synthetic end-to-end tests for receipt verification, missing-state activation, private-operation denial and restart, including rejection of normal/unrelated activation.
2. Implement guarded explicit activation and persistent restart validation with crash and concurrency tests.
3. Expose the missing state without content or identifiers; document recovery limits and the exact trusted command.
4. Verify affected source suites, installed wheel, crash windows and strict typing; commit and push this ticket and update #212/#208/#117.
