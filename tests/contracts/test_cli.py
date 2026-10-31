from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import Change, Event, MemoryRuntime, Source, initialize, pair_client
from contract_assertions import assert_original_roundtrip


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-cli-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, self.access = initialize(self.directory / "data", client_id="synthetic", entry="test")
        self.access_path = self.config.parent / "mcp-access-cli.json"
        pair_client(self.config, client_id="synthetic", entry="test", access_path=self.access_path)

    def invoke(self, command: str, payload: object = None, *, raw: bytes | None = None,
               extra: tuple[str, ...] = ()) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
        process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                                  "--access", str(self.access_path), *extra],
            input=raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""),
            capture_output=True, timeout=30, cwd=Path(__file__).resolve().parents[2])
        result: dict[str, Any] = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        self.assertNotIn(self.access.token, process.stdout.decode("utf-8"))
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return process, result

    def test_subprocess_roundtrip_retry_source_conflict_and_index_maintenance(self) -> None:
        event = Event(Source("test", "session", "main", "1"), "user", 1, "2021-02-03T10:20:30+08:00",
                      "complete", "合成：37.50元。\r\n甲🙂e\u0301乙")
        _, saved = self.invoke("remember", asdict(event))
        _, retried = self.invoke("remember", asdict(event))
        self.assertEqual(saved, retried)
        _, read = self.invoke("read", extra=("--message-id", saved["result"]["message_id"]))
        assert_original_roundtrip(self, event, saved["result"], read["result"])
        _, conflict = self.invoke("remember", asdict(replace(event, text="different")))
        self.assertEqual(conflict["error"]["code"], "source_conflict")
        _, searched = self.invoke("search", {"query": "37.50", "mode": "exact"})
        self.assertEqual(searched["result"]["hits"][0]["message_id"], saved["result"]["message_id"])
        self.assertEqual(searched["result"]["fallback_reason"], "index_not_built")
        _, refreshed = self.invoke("refresh-index")
        self.assertEqual(refreshed["result"]["state"], "ready")
        _, rebuilt = self.invoke("rebuild-index")
        self.assertEqual(rebuilt["result"]["state"], "ready")
        _, indexed = self.invoke("search", {"query": "37.50", "mode": "exact"})
        self.assertTrue(indexed["result"]["used_index"])
        self.assertEqual(indexed["result"]["hits"], searched["result"]["hits"])

    def test_sensitive_receipt_and_errors_do_not_echo_payloads_or_tokens(self) -> None:
        sentinel = "synthetic-private-value-123"
        event = Event(Source("test", "session", "main", "sensitive"), "user", 1, None, "complete", "password=" + sentinel)
        _, saved = self.invoke("remember", asdict(event))
        self.assertFalse(saved["result"]["original_saved"])
        self.assertEqual(saved["result"]["status"], "projection_only")
        _, read = self.invoke("read", extra=("--message-id", saved["result"]["message_id"]))
        self.assertNotIn(sentinel, json.dumps(read))
        for payload, raw, code in [
            (None, b"\xff", "invalid_encoding"),
            (None, ('{"text":"' + sentinel).encode(), "invalid_arguments"),
            (None, b'{"query":"one","query":"two"}', "invalid_arguments"),
            ({"query": "password=" + sentinel}, None, "protected_query"),
            ({"query": "synthetic", "token": sentinel}, None, "invalid_arguments"),
        ]:
            process, error = self.invoke("search", payload, raw=raw)
            self.assertEqual(error["error"]["code"], code)
            self.assertNotIn(sentinel.encode(), process.stdout + process.stderr)
        _, error = self.invoke("status", extra=("--token", sentinel))
        self.assertEqual(error["error"]["code"], "invalid_arguments")

    def test_strict_event_input_and_wire_limit_do_not_commit_invalid_data(self) -> None:
        event = asdict(Event(Source("test", "session", "main", "invalid"), "user", 1, None, "complete", "synthetic"))
        for payload in [{**event, "sequence": True}, {**event, "redact": False},
                        {**event, "source": {**event["source"], "extra": "synthetic"}}, []]:
            _, error = self.invoke("remember", payload)
            self.assertFalse(error["ok"])
        _, oversized = self.invoke("remember", raw=b" " * (8 * 1024 * 1024 + 1))
        self.assertEqual(oversized["error"]["code"], "input_too_large")
        self.assertEqual(MemoryRuntime(self.config).status(self.access).messages, 0)

    def test_cli_obeys_core_suppression_and_reports_pending_processing(self) -> None:
        runtime = MemoryRuntime(self.config)
        event = Event(Source("test", "session", "main", "target"), "user", 1, None, "complete", "合成待隐藏")
        target = runtime.submit(self.access, event)
        evidence = runtime.submit(self.access, replace(event, source=replace(event.source, event="evidence"), text="合成：不再召回上一条"))
        runtime.change(self.access, Change("synthetic-change", target.message_id, 1, "suppress", evidence.message_id, "synthetic"))
        _, read = self.invoke("read", extra=("--message-id", target.message_id))
        self.assertEqual(read["error"]["code"], "message_not_found")
        _, state = self.invoke("state", extra=("--message-id", target.message_id))
        self.assertEqual(state["result"]["status"], "suppressed")
        _, searched = self.invoke("search", {"query": "待隐藏"})
        self.assertEqual(searched["result"]["hits"], [])
        _, status = self.invoke("status")
        self.assertFalse(status["result"]["consumer_running"])
        self.assertFalse(status["result"]["capabilities"]["vault"])

    def test_vault_show_without_tkinter_reports_the_display_dependency_instead_of_a_generic_failure(self) -> None:
        from unittest.mock import patch

        from evernook import MemoryFailure, cli as cli_module

        arguments = cli_module._parser().parse_args(["vault-show", "--config", str(self.config),
            "--access", str(self.access_path), "--inbox-dir", str(self.directory / "inbox"),
            "--key-dir", str(self.directory / "keys"), "--object-id", "synthetic-object", "--purpose", "review"])
        # Both modules resolve to import-blocking sentinels, simulating a Python build without tkinter.
        with patch.dict(sys.modules, {"evernook.vault_local": None, "tkinter": None}), \
                patch("evernook.protected.ProtectedInbox", lambda *args, **kwargs: object()):
            with self.assertRaisesRegex(MemoryFailure, "vault_display_unavailable"):
                cli_module._execute(arguments)

    def test_init_and_pair_return_paths_only_and_pair_retries_preserve_identity(self) -> None:
        root = self.directory / "initialized"
        process = subprocess.run([sys.executable, "-m", "evernook.cli", "init", "--data-dir", str(root),
                                  "--client-id", "synthetic-owner", "--entry", "owner"], capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)["result"]
        runtime = MemoryRuntime(Path(result["config"]))
        owner = runtime.access_from_file(Path(result["access_file"]))
        self.assertNotIn(owner.token.encode(), process.stdout + process.stderr)
        peer_path = root / "mcp-access-peer.json"
        arguments = [sys.executable, "-m", "evernook.cli", "pair", "--config", result["config"],
                     "--access", str(peer_path), "--client-id", "synthetic-peer", "--entry", "peer"]
        first = subprocess.run(arguments, capture_output=True, timeout=30)
        before = peer_path.read_bytes()
        second = subprocess.run(arguments, capture_output=True, timeout=30)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(peer_path.read_bytes(), before)
        self.assertNotIn(runtime.access_from_file(peer_path).token.encode(), first.stdout + second.stdout)


if __name__ == "__main__":
    unittest.main()
