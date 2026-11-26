from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

from evernook import Access, AttachmentInput, Capture, CaptureReceipt, Change, Event, MemoryFailure, MemoryRuntime, Source, StoredMessage, initialize, pair_client
from evernook.service import ENDPOINT, ServiceClient, dispatch


class CoreHostServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="evernook-core-host-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config, self.owner = initialize(self.root / "ledger", client_id="owner", entry="owner")
        self.owner_file = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_file)
        self.writer = self.pair("dsh", ("capture",))
        self.reader = self.pair("maneki", ("recall",))

    def pair(self, name: str, scopes: tuple[str, ...]) -> Access:
        return pair_client(self.config, client_id=name, entry=name,
                           scopes=scopes, access_path=self.config.parent / f"mcp-access-{name}.json")

    def start(self) -> ServiceClient:
        repo = Path(__file__).resolve().parents[2]
        tokenizer = self.root / "tokenizer.json"
        tokenizer.write_text(json.dumps({"model": {"type": "WordLevel", "vocab": {"[UNK]": 0}, "unk_token": "[UNK]"},
            "pre_tokenizer": {"type": "Whitespace"}, "decoder": None, "normalizer": None,
            "post_processor": None, "added_tokens": []}), encoding="utf-8")
        model = self.root / "tokenizer_config.json"
        model.write_text("{}", encoding="utf-8")
        counter = self.root / "counter.json"
        counter.write_text(json.dumps({"schema_version": 1, "node": shutil.which("node"),
            "package_dir": str(repo / "plugins/harnesses/node_modules/@huggingface/tokenizers"),
            "model_id": "synthetic-host", "tokenizer_file": str(tokenizer), "config_file": str(model),
            "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256(model.read_bytes()).hexdigest()}), encoding="utf-8")
        context = self.root / "context.json"
        context.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(counter),
            "policy": {"normal_tokens": 4000, "deep_tokens": 5000, "pinned_tokens": 0,
                       "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
        # -S and an explicit source-only PYTHONPATH keep MCP, Pydantic and
        # installed project extras out of the actual serving process.
        process = subprocess.Popen([sys.executable, "-B", "-S", "-m", "evernook.service",
            "--config", str(self.config), "--activate-owner", str(self.owner_file), "--context-config", str(context)],
            cwd=self.root, env={**os.environ, "PYTHONPATH": str(repo / "src")}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop() -> None:
            process.terminate()
            process.wait(timeout=15)
            if process.stderr:
                process.stderr.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while not (self.config.parent / ENDPOINT).exists():
            if process.poll() is not None:
                self.fail(process.stderr.read().decode() if process.stderr else "service stopped")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        client = ServiceClient(self.config)
        self.addCleanup(client.close)
        return client

    @staticmethod
    def event(n: int, text: str) -> dict[str, Any]:
        return asdict(Event(Source("dsh", "synthetic", "main", str(n)), "user", n, "2025-01-01T00:00:00Z", "complete", text))

    def test_core_only_cross_client_context_and_visibility_changes(self) -> None:
        client = self.start()
        self.assertEqual(client.call(self.writer, "handshake", {})["scopes"], ["capture"])
        sender, reader = self.writer, self.reader
        as_of = "2025-01-02T00:00:00Z"
        query = {"query": "风铃", "model_id": "synthetic-host", "max_tokens": 4000, "as_of": as_of}
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            client.call(sender, "context", query)
        capture = {"kind": "message", "event": self.event(1, ""), "text_chunks": ["合成风铃", "在北窗"],
                   "attachments": [{"attachment_id": "note", "label": "note", "media_type": "text/plain",
                                    "chunks_base64": ["c2FtcGxl"], "expected_size": 6}]}
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            client.call(reader, "capture", capture)
        first = client.call(sender, "capture", capture)
        self.assertEqual(client.call(sender, "capture", capture), first)
        self.assertTrue(first["attachments"][0]["saved"])
        target = first["message"]["message_id"]
        second = client.call(sender, "capture", {"kind": "message", "event": self.event(2, "合成风铃在南窗")})["message"]["message_id"]
        self.assertEqual(client.call(reader, "read", {"message_id": target})["event"]["text"], "合成风铃在北窗")
        before = client.call(reader, "context", query)
        self.assertIn(target, before["payload"])
        self.assertIn(second, before["payload"])
        evidence = client.call(sender, "capture", {"kind": "message", "event": self.event(3, "合成变更确认")})["message"]["message_id"]
        for action, source in (("suppress", target), ("revoke", second)):
            client.call(self.owner, "cli", {"command": "change", "input": asdict(Change(
                action, source, 1, action, evidence, "合成确认"))})
            with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
                client.call(reader, "read", {"message_id": source})
        after = client.context(reader, "风铃", model_id="synthetic-host", max_tokens=4000, as_of=as_of)
        self.assertNotIn(target, after["payload"])
        self.assertNotIn(second, after["payload"])
        with self.assertRaisesRegex(MemoryFailure, "invalid_arguments"):
            client.call(reader, "context", {**query, "context_config": str(self.config)})
        with self.assertRaisesRegex(MemoryFailure, "invalid_arguments"):
            client.call(sender, "handshake", {"entry": "maneki"})
        with self.assertRaisesRegex(MemoryFailure, "invalid_(arguments|attachment)"):
            client.call(sender, "capture", {**capture, "attachments": [{"chunks": [1]}]})
        with self.assertRaisesRegex(MemoryFailure, "invalid_attachment"):
            client.call(sender, "capture", {**capture, "attachments": [{"chunks_base64": [""] * 4097}]})
        client.call(self.owner, "pair-client", {"client_id": "dsh", "entry": "dsh", "scopes": ["recall"]})
        self.assertEqual(client.call(sender, "handshake", {})["scopes"], ["recall"])
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            client.call(sender, "capture", capture)

    def test_public_client_selects_ledger_and_roundtrips_domain_types(self) -> None:
        self.assertIsNone(ServiceClient.for_ledger(self.config))
        self.assertIsInstance(ServiceClient.for_ledger(self.config, require_service=True), ServiceClient)
        self.start()
        client = ServiceClient.for_ledger(self.config, reuse=True)
        assert client is not None
        self.addCleanup(client.close)
        writer = client.access_from_file(self.config.parent / "mcp-access-dsh.json")
        self.assertEqual(client.handshake(writer)["scopes"], ["capture"])
        event = Event(Source("dsh", "synthetic", "main", "typed"), "user", 1, None, "complete", "")
        attachment = AttachmentInput("fixture", "fixture.txt", "text/plain", (b"ab", b"cd"))
        capture = Capture(event, "message", attachments=(attachment,), text_chunks=("ordinary ", "fixture"))
        receipt = client.submit_capture(writer, capture)
        self.assertIsInstance(receipt, CaptureReceipt)
        self.assertTrue(receipt.attachments[0].saved)
        self.assertEqual(receipt.attachments[0].size, 4)
        message = client.read(self.reader, receipt.message.message_id)
        self.assertIsInstance(message, StoredMessage)
        assert message is not None
        self.assertEqual(message.event.text, "ordinary fixture")
        self.assertEqual(message.receipt, receipt.message)
        self.assertIsNone(client.read(self.reader, "msg_missing"))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            client.read(writer, receipt.message.message_id)

    def test_public_client_preserves_offline_mode_and_never_replays_failed_capture(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.config.write_text(json.dumps({**config, "service_mode": True}), encoding="utf-8")
        client = ServiceClient.for_ledger(self.config)
        assert client is not None
        with self.assertRaises(MemoryFailure):
            client.handshake(self.writer)
        capture = Capture(Event(Source("dsh", "s", "b", "e"), "user", 1, None, "complete", "fixture"), "message")
        for error in ("service_unavailable", "service_canceled", "ledger_busy"):
            with patch.object(client, "call", side_effect=MemoryFailure(error)) as rpc:
                with self.assertRaisesRegex(MemoryFailure, error):
                    client.submit_capture(self.writer, capture)
                rpc.assert_called_once()
        with patch.object(client, "call", return_value={}):
            with self.assertRaisesRegex(MemoryFailure, "service_invalid_response"):
                client.submit_capture(self.writer, capture)
            with self.assertRaisesRegex(MemoryFailure, "service_invalid_response"):
                client.read(self.reader, "msg_missing")

    def test_canceled_capture_rolls_back_before_commit(self) -> None:
        runtime = MemoryRuntime(self.config)
        cancel = threading.Event()
        original = runtime._persist_event
        def interrupt(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            cancel.set()
            return result
        request = {"version": 1, "access": asdict(self.writer), "operation": "capture",
                   "arguments": {"event": self.event(1, "合成取消"), "kind": "message"}}
        with patch.object(runtime, "_persist_event", side_effect=interrupt), self.assertRaisesRegex(MemoryFailure, "capture_canceled"):
            dispatch(runtime, request, cancel=cancel)
        self.assertEqual(runtime.status(self.owner).messages, 0)
        dispatch(runtime, request)
        self.assertEqual(runtime.status(self.owner).messages, 1)

    def test_context_client_uses_legacy_adapter_only_for_explicit_unknown_operation(self) -> None:
        client = ServiceClient(self.config)
        with patch.object(client, "call", side_effect=[MemoryFailure("unknown_service_operation"), {"payload": "ok"}]) as call:
            self.assertEqual(client.context(self.reader, "合成", model_id="test", max_tokens=128,
                                           as_of="2025-01-01T00:00:00Z", transport_timeout=10), {"payload": "ok"})
            self.assertEqual([item.args[1] for item in call.call_args_list], ["context", "mcp"])
            self.assertEqual([item.kwargs["timeout"] for item in call.call_args_list], [10, 10])
        with patch.object(client, "call", side_effect=MemoryFailure("permission_denied")) as call:
            with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                client.context(self.reader, "合成", model_id="test", max_tokens=128, as_of="2025-01-01T00:00:00Z")
            self.assertEqual(call.call_count, 1)
