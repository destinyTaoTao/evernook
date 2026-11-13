# Vault reverse inventory implementation plan

> **For agentic workers:** Execute inline against synthetic vaults only, preserving the cutover refusal.

**Goal:** Identify protected ledger links and verify those addressed to one explicitly selected inbox, including the recoverable pending-acknowledgment window.

**Architecture:** An admin-only bounded keyset page reads ledger links, strictly parses reference fields and rechecks corresponding signed inbox objects against the ledger projection, fingerprint, receipt and reference. Unknown inboxes remain explicitly unverified. Missing or invalid local targets reject the page. Two stores are not an atomic snapshot; no recovery key or plaintext is accessed.

**Files:** `protected.py` holds the page and verification, `cli.py` exposes `vault-links`, `test_protected.py` exercises real ledgers and crash-state fixtures, and `protected-inbox-v1.md` states limits.

1. Write regressions for normal linkage, pagination, pending acknowledgment, foreign references, missing/tampered objects, authorization and redaction; observe failure on the previous implementation.
2. Implement bounded strict parsing and verification, keeping every failure closed and rechecking admin permission before returning.
3. Run affected tests and type checks, build a wheel, verify the new interface from a separate installed environment, document untested concurrency/recovery limits, then commit and push. Do not enable protected cutover or claim #115 complete.
