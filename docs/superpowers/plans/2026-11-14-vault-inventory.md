# Vault inventory implementation plan

> **For agentic workers:** Execute inline against synthetic vaults only; preserve the existing cutover refusal.

**Goal:** Let a trusted administrator page through verified metadata for objects in one explicitly supplied legacy inbox before cutover.

**Architecture:** Add a bounded keyset page to `ProtectedInbox` and a local `vault-inventory` CLI command. Reuse signature, key binding and linked-receipt verification. Return only existing receipt metadata. Recheck administrator scope before returning. Pages are advisory observations, not a cross-store snapshot, decryptability check, global inventory or cutover authorization.

**Files:** `protected.py` defines the page and local query; `cli.py` exposes it with cursor and limit; `test_protected.py` covers permissions, pagination, corruption and privacy; `protected-inbox-v1.md` records limitations.

1. Add regression tests for mixed pending/linked records, stable bounded pagination, non-admin refusal, invalid cursor, corrupt envelope and redacted output; confirm failure before implementation.
2. Implement the page and CLI using existing authentication, inbox binding and verification. Never load the recovery key or decrypt.
3. Run affected vault and CLI tests, strict types, and install/test a built wheel in a fresh external environment. Review the diff, record results and push one ticket commit. Keep #115 and cutover refusal open.
