# Encrypted private service frames plan

> Execute inline on isolated synthetic runtimes only; preserve all vault and ordinary-service gates.

**Goal:** Protect private capture requests and receipts on the local authenticated connection without a plaintext fallback.

**Architecture:** Extend existing nonce-based authentication to return a domain-separated per-connection key while preserving the wire handshake. Use the existing optional PyNaCl SecretBox with distinct request/response subkeys and random nonces to wrap bounded JSON. The handler accepts encrypted private capture only; ordinary operations keep their wire format. New clients fail if an old server cannot produce an encrypted response. An old client can still transmit plaintext before a new server rejects it, so deploy both sides together.

**Files:** `service_transport.py` derives and wraps frames; `service.py` applies the envelope only to protected capture; contract tests inspect actual encrypted wire values, reject plaintext/tamper/replay and test existing service flows; the protected inbox contract documents upgrade, metadata and threat boundaries.

1. Add real-process tests capturing request/response frames and checking absence of synthetic secret and Access token, plus raw plaintext rejection, tampering, cross-session replay and missing crypto. Confirm failure on the existing protocol.
2. Implement domain-separated per-session authenticated encryption using the established PyNaCl dependency. Never send private plaintext when encryption or negotiation fails; keep strict JSON and frame bounds, reject unexpected envelope operations and encrypted non-private operations.
3. Run protected service, ordinary service/bridges and transport regressions, strict mypy, and a non-editable wheel on synthetic data. Document limitations and actual commands, commit/push #155; do not claim dsh private-mode or #115 completion.
