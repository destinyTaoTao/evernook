from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, configure_audit, configure_lockout, initialize, pair_client, unlock_pairing
from evernook.context import ContextBuilder, ContextPolicy
from tests.contracts.test_context import SyntheticCounter


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-audit-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, _ = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.access = self.runtime.access_from_file(self.owner_path)
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        from evernook import HARNESS_DEFAULT_SCOPES
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.maintainer_path = self.config.parent / "mcp-access-maintainer.json"
        self.maintainer_access = pair_client(self.config, client_id="maintainer", entry="maintainer",
                                             access_path=self.maintainer_path,
                                             scopes=("recall", "capture", "maintain", "accept"))
        self.number = 0

    def invoke(self, command: str, payload: object = None, extra: tuple[str, ...] = ()) -> dict[str, Any]:
        arguments = [sys.executable, "-m", "evernook.cli", command]
        if "--config" not in extra:
            arguments += ["--config", str(self.config)]
        if command in ("audit-query", "audit-health") and "--access" not in extra:
            arguments += ["--access", str(self.maintainer_path)]
        process = subprocess.run([*arguments, *extra],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"",
            capture_output=True, timeout=30, cwd=Path(__file__).resolve().parents[2])
        result: dict[str, Any] = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return result

    def test_caller_operations_leave_append_only_audit_records(self) -> None:
        event = Event(Source("harness", "session", "main", "a1"), "user", 1, None, "complete", "合成：审计样本")
        receipt = self.runtime.submit(self.harness, event)
        self.runtime.read(self.harness, receipt.message_id)
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.runtime.change(self.harness, __import__("evernook").Change("audit-denied", receipt.message_id,
                1, "suppress", receipt.message_id, "合成"))
        page = self.runtime.audit_query(self.access, client_id="harness")
        rows = [(record.action, record.outcome) for record in page.records]
        self.assertIn(("submit", "ok"), rows)
        self.assertIn(("read", "ok"), rows)
        self.assertIn(("change", "denied"), rows)
        targets = {record.target for record in page.records if record.action == "submit"}
        self.assertEqual(targets, {'{"branch":"main","entry":"harness","event":"a1","session":"session"}'})
        seqs = [record.seq for record in page.records]
        self.assertEqual(seqs, sorted(seqs))

    def test_audit_query_filters_and_requires_maintain_scope(self) -> None:
        self.runtime.search(self.harness, "合成", mode="lexical")
        self.runtime.search(self.access, "合成", mode="lexical")
        harness_only = self.runtime.audit_query(self.access, client_id="harness")
        self.assertTrue(harness_only.records)
        self.assertEqual({record.client_id for record in harness_only.records}, {"harness"})
        since = harness_only.records[0].at
        windowed = self.runtime.audit_query(self.access, since=since)
        self.assertTrue(windowed.records)
        empty = self.runtime.audit_query(self.access, until="2000-01-01T00:00:00+00:00")
        self.assertEqual(empty.records, ())
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.runtime.audit_query(self.harness)
        cli_page = self.invoke("audit-query", {"client_id": "harness"})
        self.assertTrue(cli_page["result"]["records"])

    def test_composed_read_has_one_diagnostic_transaction_per_public_operation(self) -> None:
        for i in range(4):
            self.runtime.submit(self.harness, Event(Source("harness", "session", "main", str(i)),
                "user", i, "2026-10-01T00:00:00+00:00", "complete", "synthetic audit sample"))
        self.runtime.refresh_lexical(self.access, limit=10)
        recall = SemanticRecall(self.runtime)
        builder = ContextBuilder(self.runtime, recall, ContextPolicy(20000, 30000, 0, 7, 8192), SyntheticCounter())
        sql: list[str] = []
        connect = sqlite3.connect
        def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = connect(*args, **kwargs)
            db.set_trace_callback(sql.append)
            return db
        with patch("sqlite3.connect", traced):
            self.assertEqual(len(recall.recall(self.harness, "synthetic").hits), 4)
            bundle = builder.build(self.harness, "synthetic", as_of="2026-10-02T00:00:00+00:00",
                mode="normal", timeout=10)
            self.assertEqual(bundle.evidence_count, 4)
        self.assertEqual(sql.count("BEGIN IMMEDIATE"), 2)
        self.assertFalse(any(line.startswith(("CREATE", "DELETE")) and "audit_" in line for line in sql))
        records = self.runtime.audit_query(self.access, client_id="harness").records
        self.assertEqual([(r.action, r.outcome) for r in records[-2:]], [("recall", "ok"), ("context_build", "ok")])

    def test_revoke_recall_during_final_tokenization_prevents_handoff_and_records_denial(self) -> None:
        counter = SyntheticCounter()
        def revoke(text: str) -> int:
            pair_client(self.config, client_id="harness", entry="harness", access_path=self.harness_path,
                        scopes=("capture",))
            return len(text)
        builder = ContextBuilder(self.runtime, SemanticRecall(self.runtime),
                                 ContextPolicy(4000, 8000, 0, 7, 8192), counter)
        with patch.object(counter, "count", side_effect=revoke), self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            builder.build(self.harness, "", as_of="2026-10-02T00:00:00+00:00", mode="normal", timeout=10)
        rows = self.runtime.audit_query(self.access, client_id="harness").records
        self.assertEqual([row.outcome for row in rows if row.action == "context_build"], ["denied"])

    def test_failed_submissions_never_leave_success_rows_for_rolled_back_sources(self) -> None:
        event = Event(Source("harness", "session", "main", "one"), "user", 1, None, "complete", "synthetic audit")
        receipt = self.runtime.submit(self.harness, event)
        conflict = replace(event, text="another synthetic text")
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            self.runtime.submit(self.harness, conflict)
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            self.runtime.submit_many(self.harness, (replace(event, source=replace(event.source, event="two")), conflict))
        rows = self.runtime.audit_query(self.access, client_id="harness").records
        self.assertEqual([(r.action, r.outcome) for r in rows if r.action.startswith("submit")],
                         [("submit", "ok"), ("submit", "source_conflict"), ("submit_many", "source_conflict")])
        original = self.runtime.read_source(self.harness, event.source)
        assert original is not None
        self.assertEqual(original.receipt, receipt)

    def test_unavailable_diagnostics_leave_single_receipt_and_read_result_usable(self) -> None:
        event = Event(Source("harness", "session", "main", "busy"), "user", 1, None, "complete", "synthetic")
        with self.runtime._connection() as db:
            db.execute("""CREATE TRIGGER reject_diagnostic BEFORE INSERT ON audit_events_v1
                BEGIN SELECT RAISE(FAIL, 'synthetic audit failure'); END""")
        receipt = self.runtime.submit(self.harness, event)
        with self.runtime._connection() as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            recalled = SemanticRecall(self.runtime).recall(self.harness, "synthetic")
            self.assertEqual([hit.evidence.message_id for hit in recalled.hits], [receipt.message_id])
            self.assertEqual(self.runtime.audit_health(self.maintainer_access), {"dropped_busy": 1, "dropped_other": 1})
        records = self.runtime.audit_query(self.access, client_id="harness").records
        self.assertFalse(any(row.action in {"submit", "recall"} for row in records))

    def test_activated_maintenance_cli_counts_lost_reads_and_resets_on_service_restart(self) -> None:
        from evernook.service import ENDPOINT, ServiceClient
        event = Event(Source("harness", "fixture", "main", "1"), "user", 1, None, "complete", "synthetic private payload")
        saved = self.runtime.submit(self.harness, event)
        self.assertEqual(self.invoke("audit-health")["result"], {"dropped_busy": 0, "dropped_other": 0})
        endpoint = self.config.parent / ENDPOINT

        @contextmanager
        def running_service(*options: str) -> Iterator[None]:
            previous = endpoint.read_bytes() if endpoint.exists() else None
            with subprocess.Popen([sys.executable, "-m", "evernook.service", "--config", str(self.config), *options],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
                try:
                    deadline = time.monotonic() + 15
                    while not endpoint.exists() or endpoint.read_bytes() == previous:
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.03)
                    yield
                finally:
                    process.terminate()
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(stderr, b"")

        def read(_: int) -> str:
            reply = ServiceClient(self.config).call(self.harness, "read", {"message_id": saved.message_id})
            return str(reply["event"]["text"])

        def audit_rows() -> list[dict[str, Any]]:
            result = self.invoke("audit-query", {"client_id": "harness"}, extra=("--require-service",))
            encoded = json.dumps(result)
            for secret in (event.text, self.access.token, self.harness.token, self.maintainer_access.token,
                           self.directory.name):
                self.assertNotIn(secret, encoded)
            return list(result["result"]["records"])

        with running_service("--activate-owner", str(self.owner_path)):
            with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                with ThreadPoolExecutor(max_workers=4) as pool:
                    self.assertEqual(list(pool.map(read, range(12))), [event.text] * 12)
                for _ in range(2):
                    self.assertEqual(self.invoke("audit-health", extra=("--require-service",))["result"],
                                     {"dropped_busy": 12, "dropped_other": 0})
            for command in ("audit-query", "audit-health"):
                denied = self.invoke(command, {}, extra=("--access", str(self.harness_path), "--require-service"))
                self.assertEqual(denied["error"], {"code": "permission_denied"})
            self.assertFalse(any(row["action"] == "read" for row in audit_rows()))
            self.assertEqual(read(0), event.text)
            self.assertEqual(sum(row["action"] == "read" for row in audit_rows()), 1)
        with running_service():
            self.assertEqual(self.invoke("audit-health", extra=("--require-service",))["result"],
                             {"dropped_busy": 0, "dropped_other": 0})
            self.assertEqual(sum(row["action"] == "read" for row in audit_rows()), 1)

    def test_old_schema_reopens_and_expired_rows_are_hidden_during_bounded_cleanup(self) -> None:
        with self.runtime._connection() as db:
            db.execute("DROP TABLE audit_settings_v1")
            db.execute("DROP TABLE audit_events_v1")
        migrated = MemoryRuntime(self.config)
        migrated.read(self.access, "missing")
        with migrated._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO audit_events_v1(at,client_id,entry,action,target,outcome) VALUES (?,?,?,?,?,?)",
                           [("2000-01-01T00:00:00+00:00", "expired", "", "read", "", "ok")] * 300)
            db.execute("COMMIT")
        reopened = MemoryRuntime(self.config)
        reopened.read(self.access, "missing")
        with reopened._connection() as db:
            remaining = db.execute("SELECT count(*) FROM audit_events_v1 WHERE client_id='expired'").fetchone()[0]
        self.assertEqual(remaining, 44)
        self.assertEqual(reopened.audit_query(self.access, client_id="expired").records, ())
        configure_audit(self.config, retention_days=1)
        with reopened._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events_v1 WHERE client_id='expired'").fetchone()[0], 0)

    def test_pairing_failures_lock_and_trusted_unlock_restores(self) -> None:
        configure_lockout(self.config, failure_limit=3)
        for _ in range(3):
            with self.assertRaisesRegex(MemoryFailure, "client_conflict"):
                pair_client(self.config, client_id="intruder", entry="owner",
                            access_path=self.config.parent / "mcp-access-intruder.json")
        with self.assertRaisesRegex(MemoryFailure, "pairing_locked"):
            pair_client(self.config, client_id="victim", entry="fresh-entry",
                        access_path=self.config.parent / "mcp-access-victim.json")
        lock_page = self.runtime.audit_query(self.access, client_id="victim")
        self.assertEqual([record.outcome for record in lock_page.records], ["locked"])
        unlock_pairing(self.config)
        fresh_path = self.config.parent / "mcp-access-fresh.json"
        pair_client(self.config, client_id="fresh", entry="fresh-entry", access_path=fresh_path)
        runtime = MemoryRuntime(self.config)
        access = runtime.access_from_file(fresh_path)
        self.assertEqual(runtime._authorize(access).client_id, "fresh")
        unlock_page = self.runtime.audit_query(self.access, client_id="local-operator")
        self.assertIn("pair_unlock", [record.action for record in unlock_page.records])

    def test_lockout_resets_after_successful_pairing_and_threshold_configurable(self) -> None:
        configure_lockout(self.config, failure_limit=2)
        with self.assertRaisesRegex(MemoryFailure, "client_conflict"):
            pair_client(self.config, client_id="probe", entry="owner",
                        access_path=self.config.parent / "mcp-access-probe.json")
        good_path = self.config.parent / "mcp-access-good.json"
        pair_client(self.config, client_id="good", entry="good-entry", access_path=good_path)
        with self.assertRaisesRegex(MemoryFailure, "client_conflict"):
            pair_client(self.config, client_id="probe", entry="owner",
                        access_path=self.config.parent / "mcp-access-probe.json")
        with self.assertRaisesRegex(MemoryFailure, "invalid_lockout_config"):
            configure_lockout(self.config, failure_limit=0)

    def test_audit_retention_configurable_and_prunes_old_rows(self) -> None:
        configure_audit(self.config, retention_days=1)
        self.runtime.read(self.access, "synthetic-missing")
        page = self.runtime.audit_query(self.access)
        self.assertTrue(page.records)
        with self.assertRaisesRegex(MemoryFailure, "invalid_audit_config"):
            configure_audit(self.config, retention_days=0)
        cli = self.invoke("audit-configure", extra=("--retention-days", "30"))
        self.assertEqual(cli["result"]["retention_days"], 30)


if __name__ == "__main__":
    unittest.main()
