# Service activation safety plan

> **For agentic workers:** Apply this plan inline; do not change protected assets or enable a daily installation.

**Goal:** Make an unactivated service fail closed by default while retaining an explicitly named compatibility mode for isolated legacy tests.

**Architecture:** Under the existing service instance lock and exclusive ownership gate, check the persisted mode. A first normal startup needs an admin activation; an activated ledger can restart without credentials. The compatibility flag is opt-in, reports that single-writer protection is absent, and cannot start when the ordinary ledger contains protected links. Independent protected inboxes cannot be discovered from the ledger, so this is not a migration check.

**Files:** `service.py` implements the gate and flag; `test_service_cutover.py` exercises real startup and existing asset refusal; legacy service fixture modules opt into their deliberate shared test mode; `service-v1.md` documents changed startup and the limited compatibility mode. No cryptographic transport, dsh capture, backup, or private-session behavior is introduced.

1. Add a real subprocess regression for a fresh ledger: normal startup fails without activation or endpoint publication; explicit compatibility mode can start, but reports its weaker mode. Confirm the regression fails on the existing implementation.
2. Implement the check without changing existing admin activation, protected-link rejection, ownership lock ordering, or activated restarts. Fail closed on invalid combinations.
3. Update only test fixtures that intentionally exercise pre-cutover shared mode. Preserve the activated cutover/concurrency tests and do not silently rename or delete existing assertions.
4. Run service, cutover, bridge, maintenance and MCP contract tests as appropriate, type/build checks, then validate a non-editable wheel on a fresh synthetic ledger. Review the diff, document compatibility and pending-inbox limitations, and commit/push the ticket.
