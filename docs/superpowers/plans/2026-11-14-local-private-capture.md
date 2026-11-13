# Local private capture window plan

> Execute with synthetic secrets and isolated runtimes. This is not a private dsh chat or an authenticated disclosure UI.

**Goal:** A person can enter a complete private note in a dedicated local window and explicitly submit it to the configured encrypted service before host chat, transcription or model processing.

**Architecture:** A local capture request owns non-secret generated source identifiers and freezes the first submitted text for idempotent retries. It calls only `ServiceClient.protected_receive`; failures never fall back, concurrent submissions are rejected, and closing during an in-flight request reports an uncertain result. A Tk window handles text input directly, clears widgets on close/focus loss, polls the existing conservative input-desktop check, and displays only fixed status messages. A dedicated CLI launches the window and returns non-secret metadata; no text travels via arguments, stdout or chat. GUI/OS memory, IME, screen capture and lock-screen limitations remain explicit.

**Files:** new `private_capture_local.py` owns the state machine and window; `cli.py` exposes `service-private-window`; contract tests exercise retry/concurrency, failure privacy and actual service and Tk boundaries; docs record use and limitations.

1. Test a failing first submit and exact retry, concurrent attempts, changed-content refusal and no secret in result/errors. Verify real service stores only ciphertext and ordinary projection.
2. Implement the state machine and local window, then test close/focus/cancel semantics with synthetic values and real Tk where available. No automated test should imply human authentication or OS lock acceptance.
3. Run relevant service/vault/CLI tests and strict types, build and install a wheel, verify a synthetic native UI in an isolated profile, document actual results, and commit/push #156. Keep #115 open.
