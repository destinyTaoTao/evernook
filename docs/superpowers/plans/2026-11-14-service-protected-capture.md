# Trusted local protected service capture plan

> Execute with isolated synthetic ledgers and inboxes; do not switch a user's installation.

**Goal:** An explicitly configured independent service can encrypt individually marked private text submitted by a trusted local client while locked, with no ordinary plaintext persistence or model tool exposure.

**Boundary:** Authentication is local paired-client scope and source entry; loopback frames are authenticated but not encrypted. This is a trusted capture API, not dsh private chat, identity-authenticated disclosure, streams, attachments, migration or recovery. No cloud fallback or ordinary plaintext retry.

**Design:** `protected.py` acquires the existing shared ownership gate around staging and rechecks capture scope inside it. At activation, `service.py` binds startup-selected inbox/key directories, checks the inbox is empty inside the exclusive gate and refuses prior protected assets; no caller-selected paths. A dedicated service operation seals and links explicitly sensitive complete events; errors and ambiguous acknowledgements do not trigger plaintext fallback. A dedicated CLI uses service transport and stdin; MCP tool lists remain unchanged. The service does not load the recovery key.

1. Add real-service tests for disabled/configured capture, scope/entry checks, explicit sensitive marking, idempotency, restart and sentinel exclusion, plus a stage/activation race and pre-existing pending refusal. Demonstrate failure before implementation.
2. Implement the locked staging and startup binding, service operation and CLI; keep paths trusted-startup-only and all other paths unchanged. Verify failure and no-plaintext behavior under missing or corrupted materials.
3. Run affected service, protected, cutover and CLI tests; build/install a fresh wheel and run the real subprocess scenarios. Document transport, host, incomplete stream, attachment and migration limits; do not close #115. Review, commit and push #154 only when all its tests pass.
