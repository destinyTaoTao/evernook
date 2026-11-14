# Offline Backup Recovery Implementation Plan

**Goal:** Restore a verified ordinary backup on another machine after explicit review and confirmation, with known snapshot controls retained and old clients replaced.

**Architecture:** Build on the existing authenticated encrypted backup and archive verification rather than bypassing them. Separate authenticated review from explicit publication, bind confirmation to immutable verified content, and atomically publish a new private runtime with fresh owner credentials. Treat protected asset backup and a safe missing-vault service state as explicit dependencies, not hidden exclusions.

**Tech Stack:** Python 3.14, SQLite, existing PyNaCl backup format, trusted CLI and unittest.

**Spec:** `docs/superpowers/specs/2026-11-14-offline-backup-recovery-design.md`; #117 and #208.

## Constraints and risks

- Never change the existing review-only restore or weaken cutover/security gates.
- Disclose that post-snapshot controls are unknown; no automatic release based on authentication alone.
- Keys and passwords stay out of RPC, CLI output, logging and test fixtures committed to the repository.
- No use of real user data or destructive operations on existing user paths.
- Backups containing protected references cannot silently lose them; the service activation dependency must be resolved and tested.
- Pending operations, model admission and old clients require explicit new-machine treatment.

## Tasks

1. Characterize v1 backup authenticity and all state retained in the SQLite snapshot, including control, durable operation, model admission and vault tables; build regression fixtures for stale controls and absent original machine.
2. Implement one authenticated review and revalidation flow using the existing format; reject invalid/mismatched keys, archive HMAC and ledger identity. Test secret-free reports and mutation races.
3. Implement trusted confirmation and atomic publication with fresh identity and crash/idempotence handling; test lost response, old tokens, stale snapshots and independent service restart.
4. Resolve the service state for missing separately protected assets, then implement and test opt-in protected backup/restore and association verification. Never expose locked secrets as ordinary memory.
5. Run existing backup/cutover regression, source and installed-package end-to-end tests; update contracts and #117/#208 with real results and remaining physical-device limitations. Commit and push independently verified tickets.
