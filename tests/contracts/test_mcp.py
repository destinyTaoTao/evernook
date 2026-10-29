from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import subprocess
import sqlite3
import tempfile
import unittest
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from contract_assertions import assert_original_roundtrip


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-mcp-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.access_path = self.config.parent / "mcp-access.json"
        self.access_path.write_text(json.dumps(asdict(self.access)), encoding="utf-8")
        self.parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "evernook.mcp_server", "--config", str(self.config), "--access", str(self.access_path)],
            cwd=str(Path(__file__).resolve().parents[2]))

    async def test_real_stdio_protocol_saves_exact_original_and_reads_same_core_record(self) -> None:
        event = Event(Source("test", "session", "main", "1"), "user", 1, "2021-02-03T10:20:30+08:00",
                      "complete", "合成：37.50元。\r\n甲🙂e\u0301乙")
        async with Client(self.parameters, read_timeout_seconds=20) as client:
            tools = await client.list_tools()
            self.assertIn("evernook_v1_remember", [tool.name for tool in tools.tools])
            result = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertFalse(result.is_error)
            payload = result.structured_content
            assert payload is not None
            self.assertTrue(payload["ok"])
            message_id = payload["result"]["message_id"]
            read = await client.call_tool("evernook_v1_read", {"message_id": message_id})
            assert read.structured_content is not None
            self.assertEqual(read.structured_content["result"]["event"]["text"], event.text)
            assert_original_roundtrip(self, event, payload["result"], read.structured_content["result"])
        stored = MemoryRuntime(self.config).read(self.access, message_id)
        assert stored is not None
        self.assertEqual(stored.event, event)

    async def test_protocol_important_change_time_and_status_reuse_core_semantics(self) -> None:
        event = Event(Source("test", "session", "main", "1"), "user", 1, "2021-02-03T10:20:30+08:00",
                      "complete", "甲🙂e\u0301乙，37.50元。")
        async with Client(self.parameters, read_timeout_seconds=20) as client:
            saved = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
            assert saved.structured_content is not None
            receipt = saved.structured_content["result"]
            marked = await client.call_tool("evernook_v1_mark_important", {"mark": {
                "message_id": receipt["message_id"], "representation": "original", "content_version": 1,
                "scope": "synthetic-shared", "start": 1, "end": 4, "expected_text": "🙂e\u0301"}})
            self.assertFalse(marked.is_error)
            assert marked.structured_content is not None
            item = marked.structured_content["result"]
            page = await client.call_tool("evernook_v1_read_important", {"important_id": item["important_id"], "limit": 2})
            assert page.structured_content is not None
            self.assertEqual(page.structured_content["result"]["text"], "🙂e")
            listing = await client.call_tool("evernook_v1_list", {"since": "2021-02-03T02:20:30Z", "until": "2021-02-03T02:20:31Z"})
            assert listing.structured_content is not None
            self.assertEqual(len(listing.structured_content["result"]["messages"]), 1)
            evidence = Event(Source("test", "session", "main", "2"), "user", 2, None, "complete", "合成：不再召回先前原话。")
            saved_evidence = await client.call_tool("evernook_v1_remember", {"event": asdict(evidence)})
            assert saved_evidence.structured_content is not None
            request = {"request_id": "synthetic-change", "target_id": receipt["message_id"], "expected_version": 1,
                "action": "suppress", "evidence_id": saved_evidence.structured_content["result"]["message_id"], "reason": "synthetic suppression"}
            changed = await client.call_tool("evernook_v1_change", {"change": request})
            assert changed.structured_content is not None
            self.assertEqual(changed.structured_content["result"]["status"], "suppressed")
            retry = await client.call_tool("evernook_v1_change", {"change": request})
            self.assertEqual(retry.structured_content, changed.structured_content)
            stale = await client.call_tool("evernook_v1_change", {"change": {**request, "request_id": "synthetic-late"}})
            self.assertEqual(stale.structured_content, {"contract_version": 1, "ok": False, "error": {"code": "version_conflict"}})
            hidden = await client.call_tool("evernook_v1_read_important", {"important_id": item["important_id"]})
            assert hidden.structured_content is not None
            self.assertTrue(hidden.is_error)
            self.assertEqual(hidden.structured_content["error"]["code"], "not_recallable")
            status = await client.call_tool("evernook_v1_status", {})
            assert status.structured_content is not None
            self.assertEqual(status.structured_content["result"]["ledger"]["messages"], 2)
            self.assertEqual(status.structured_content["result"]["ledger"]["pending_jobs"], 2)
            self.assertNotIn(self.access.token, json.dumps(status.structured_content))

    async def test_two_explicitly_paired_clients_share_one_ledger_and_cannot_spoof_each_others_source(self) -> None:
        peer_path = self.config.parent / "mcp-access-peer.json"
        peer = pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=peer_path)
        peer_parameters = self.parameters.model_copy(update={"args": ["-m", "evernook.mcp_server", "--config",
            str(self.config), "--access", str(peer_path)]})
        async with Client(self.parameters, read_timeout_seconds=20) as first:
            async with Client(peer_parameters, read_timeout_seconds=20) as second:
                event = Event(Source("test", "session", "main", "shared"), "user", 1, None, "complete", "synthetic shared original")
                saved = await first.call_tool("evernook_v1_remember", {"event": asdict(event)})
                assert saved.structured_content is not None
                message_id = saved.structured_content["result"]["message_id"]
                shared = await second.call_tool("evernook_v1_read", {"message_id": message_id})
                assert shared.structured_content is not None
                self.assertEqual(shared.structured_content["result"]["event"]["text"], event.text)
                forged = await second.call_tool("evernook_v1_remember", {"event": asdict(event)})
                assert forged.structured_content is not None
                self.assertTrue(forged.is_error)
                self.assertEqual(forged.structured_content["error"]["code"], "unauthorized_source")
                peer_event = Event(Source("peer-entry", "session", "main", "shared"), "user", 1, None, "complete", event.text)
                separate = await second.call_tool("evernook_v1_remember", {"event": asdict(peer_event)})
                assert separate.structured_content is not None
                self.assertNotEqual(separate.structured_content["result"]["message_id"], message_id)
        core = MemoryRuntime(self.config)
        self.assertEqual(core.status(self.access).messages, 2)
        self.assertEqual(core.status(peer).messages, 2)
        self.assertEqual(core.status(peer).pending_jobs, 2)

    async def test_sensitive_projection_strict_arguments_and_errors_never_echo_to_diagnostics(self) -> None:
        sentinel = "synthetic-private-value-123"
        event = Event(Source("test", "session", "main", "sensitive"), "user", 1, None, "complete", "password=" + sentinel)
        stderr_path = self.config.parent / "protocol-stderr.txt"
        with stderr_path.open("w+", encoding="utf-8") as diagnostics:
            async with Client(stdio_client(self.parameters, errlog=diagnostics), read_timeout_seconds=20) as client:
                saved = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
                assert saved.structured_content is not None
                self.assertFalse(saved.structured_content["result"]["original_saved"])
                message_id = saved.structured_content["result"]["message_id"]
                read = await client.call_tool("evernook_v1_read", {"message_id": message_id})
                self.assertNotIn(sentinel, json.dumps(read.structured_content))
                requests: list[tuple[str, dict[str, Any], str]] = [
                    ("evernook_v1_remember", {"event": asdict(event), "access": {"token": sentinel}}, "invalid_arguments"),
                    ("evernook_v1_remember", {"event": {**asdict(event), "redact": False}}, "invalid_arguments"),
                    ("evernook_v1_remember", {"event": {**asdict(event), "sequence": True}}, "invalid_arguments"),
                    ("evernook_v1_read", {"message_id": "missing"}, "message_not_found"),
                    ("evernook_v1_read_credentials", {"secret": sentinel}, "unknown_tool"),
                ]
                for tool, arguments, code in requests:
                    result = await client.call_tool(tool, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content, {"contract_version": 1, "ok": False, "error": {"code": code}})
                    self.assertNotIn(sentinel, str(result))
            diagnostics.seek(0)
            stderr = diagnostics.read()
            self.assertNotIn(sentinel, stderr)
            self.assertNotIn(self.access.token, stderr)
        self.assertEqual(MemoryRuntime(self.config).status(self.access).messages, 1)

    def test_startup_refuses_wrong_token_version_and_unsafe_access_file_permissions(self) -> None:
        original = asdict(self.access)
        for key, value, code in [("token", "wrong", "unauthorized"), ("contract_version", 2, "incompatible_contract")]:
            self.access_path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
            result = subprocess.run([self.parameters.command, *self.parameters.args], input="", capture_output=True,
                                    text=True, timeout=20, cwd=self.parameters.cwd)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.strip(), "evernook:" + code)
            self.assertNotIn(self.access.token, result.stderr)
        self.access_path.write_text(json.dumps(original), encoding="utf-8")
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        subprocess.run([str(icacls), str(self.access_path), "/grant", "*S-1-1-0:R"], capture_output=True, check=True, timeout=10)
        result = subprocess.run([self.parameters.command, *self.parameters.args], input="", capture_output=True,
                                text=True, timeout=20, cwd=self.parameters.cwd)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "evernook:unsafe_runtime_permissions")

    async def test_sqlite_busy_and_failed_insert_return_explicit_protocol_errors_and_safe_retry(self) -> None:
        configuration = json.loads(self.config.read_text(encoding="utf-8"))
        configuration["busy_timeout_ms"] = 0
        self.config.write_text(json.dumps(configuration), encoding="utf-8")
        event = Event(Source("test", "session", "main", "failure"), "user", 1, None, "complete", "synthetic retry event")
        async with Client(self.parameters, read_timeout_seconds=20) as client:
            with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as lock:
                lock.execute("BEGIN IMMEDIATE")
                busy = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
                self.assertEqual(busy.structured_content, {"contract_version": 1, "ok": False, "error": {"code": "storage_busy"}})
            with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
                fault.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'synthetic error'); END")
            failed = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertEqual(failed.structured_content, {"contract_version": 1, "ok": False, "error": {"code": "storage_unavailable"}})
            status = await client.call_tool("evernook_v1_status", {})
            assert status.structured_content is not None
            self.assertEqual(status.structured_content["result"]["ledger"]["messages"], 0)
            self.assertEqual(status.structured_content["result"]["ledger"]["pending_jobs"], 0)
            with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
                fault.execute("DROP TRIGGER synthetic_failure")
            saved = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
            repeated = await client.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertFalse(saved.is_error)
            self.assertEqual(repeated.structured_content, saved.structured_content)
            conflict = await client.call_tool("evernook_v1_remember", {"event": asdict(replace(event, text="different"))})
            self.assertEqual(conflict.structured_content, {"contract_version": 1, "ok": False, "error": {"code": "source_conflict"}})
        async with Client(self.parameters, read_timeout_seconds=20) as restarted:
            recovered = await restarted.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertEqual(recovered.structured_content, saved.structured_content)

    async def test_live_identity_token_changes_are_checked_on_each_protocol_operation(self) -> None:
        async with Client(self.parameters, read_timeout_seconds=20) as client:
            allowed = await client.call_tool("evernook_v1_status", {})
            self.assertFalse(allowed.is_error)
            path = self.config.parent / "client.json"
            credentials = json.loads(path.read_text(encoding="utf-8"))
            credentials["token"] = "synthetic-disabled-token-material-with-length-over-forty"
            path.write_text(json.dumps(credentials), encoding="utf-8")
            denied = await client.call_tool("evernook_v1_status", {})
            self.assertEqual(denied.structured_content, {"contract_version": 1, "ok": False, "error": {"code": "unauthorized"}})

    async def test_legacy_initialize_client_uses_same_versioned_tools_and_receipt(self) -> None:
        event = Event(Source("test", "session", "main", "legacy"), "user", 1, None, "complete", "synthetic legacy protocol")
        async with Client(self.parameters, mode="legacy", read_timeout_seconds=20) as legacy:
            self.assertNotEqual(legacy.protocol_version, "2026-07-28")
            saved = await legacy.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertFalse(saved.is_error)
        async with Client(self.parameters, read_timeout_seconds=20) as modern:
            repeated = await modern.call_tool("evernook_v1_remember", {"event": asdict(event)})
            self.assertEqual(repeated.structured_content, saved.structured_content)

    def test_raw_stdio_rejects_invalid_utf8_without_committing_replacement_characters(self) -> None:
        event = Event(Source("test", "session", "main", "invalid-wire"), "user", 1, None, "complete", "synthetic UTF8_SENTINEL original")
        with subprocess.Popen([self.parameters.command, *self.parameters.args], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.parameters.cwd) as process:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            with ThreadPoolExecutor(max_workers=1) as reader:
                try:
                    initialization = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "synthetic", "version": "1"}}}
                    process.stdin.write(json.dumps(initialization).encode() + b"\n")
                    process.stdin.flush()
                    initialized = json.loads(reader.submit(process.stdout.readline).result(timeout=20))
                    self.assertIn("result", initialized)
                    process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "evernook_v1_remember", "arguments": {"event": asdict(event)}}}
                    process.stdin.write(json.dumps(request).encode().replace(b"UTF8_SENTINEL", b"\xff") + b"\n")
                    process.stdin.flush()
                    line = reader.submit(process.stdout.readline).result(timeout=20)
                    response = json.loads(line) if line else {}
                    self.assertFalse(response.get("result", {}).get("structuredContent", {}).get("ok", False))
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                diagnostics = process.stderr.read().decode("utf-8", errors="strict")
                self.assertEqual(diagnostics.strip(), "evernook:invalid_encoding")
                self.assertNotIn(self.access.token, diagnostics)
        self.assertEqual(MemoryRuntime(self.config).status(self.access).messages, 0)


if __name__ == "__main__":
    unittest.main()
