from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import Event, MemoryFailure, MemoryRuntime, Source, configure_audit, configure_lockout, initialize, pair_client, unlock_pairing


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
        if command == "audit-query":
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
