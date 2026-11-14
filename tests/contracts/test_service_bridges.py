from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import argparse
import io
from types import SimpleNamespace
import threading
from typing import Any
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from evernook import Event, Source, initialize, pair_client
from evernook.service import ServiceClient
from evernook.runtime import MemoryRuntime
from evernook.service import _Server
import test_tokenizer


class ServiceBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-service-bridge-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data",
            client_id="synthetic", entry="owner")
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        self.owner_path.write_text(json.dumps(asdict(self.access)), encoding="utf-8")
        self.ordinary_path = self.config.parent / "mcp-access-ordinary.json"
        pair_client(self.config, client_id="ordinary", entry="test", access_path=self.ordinary_path,
            scopes=("recall", "capture"))
        self.service = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config), "--allow-legacy-shared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 20
        while not (self.config.parent / "memory-service.json").exists():
            if self.service.poll() is not None or time.monotonic() > deadline:
                self.fail("service startup failed")
            time.sleep(0.05)
        self.client = ServiceClient(self.config)
        self.parameters = StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "evernook.mcp_server", "--config", str(self.config),
                "--access", str(self.ordinary_path), "--profile", "trusted-host"], env=dict(os.environ))

    def stop(self) -> None:
        if self.service.poll() is None:
            self.service.terminate()
        self.service.wait(timeout=15)

    def test_required_cli_never_falls_back_if_discovery_file_disappears(self) -> None:
        from evernook.cli import main
        from evernook.models import MemoryFailure
        args = argparse.Namespace(command="manage", config=self.config,
            access=self.owner_path, require_service=True)
        fake_runtime = SimpleNamespace(access_from_file=lambda path: self.access)
        with (
            patch("evernook.cli.Parser.parse_args", return_value=args),
            patch("pathlib.Path.exists", side_effect=[True, False]),
            patch("evernook.cli.MemoryRuntime", return_value=fake_runtime),
            patch("evernook.cli._execute", return_value={}) as direct,
            patch("evernook.cli._input", return_value={}),
            patch("evernook.service.ServiceClient.call", side_effect=MemoryFailure("service_unavailable")),
            patch("sys.stdout", SimpleNamespace(buffer=io.BytesIO())),
        ):
            with self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 1)
            direct.assert_not_called()

    async def test_mcp_preserves_long_operation_budget_in_service_transport(self) -> None:
        from mcp import types
        from mcp.server import Server
        import evernook.mcp_server as adapter
        callbacks: dict[str, Any] = {}
        original_server = Server
        def capture(*args: Any, **kwargs: Any) -> Any:
            callbacks.update(kwargs)
            return original_server(*args, **kwargs)
        with patch("evernook.mcp_server.Server", side_effect=capture):
            adapter.build_server(MemoryRuntime(self.config), self.access,
                profile="trusted-host", service_config=self.config)
        with patch("evernook.service.ServiceClient.call", return_value={}) as remote:
            result = await callbacks["on_call_tool"](None, types.CallToolRequestParams(
                name="evernook_v1_search", arguments={"query": "hello", "timeout": 120}))
            self.assertFalse(result.is_error)
            self.assertEqual(remote.call_args.kwargs.get("timeout"), 130)

    async def test_real_mcp_and_cli_share_service_and_fail_closed_offline(self) -> None:
        event = asdict(Event(Source("test", "bridge", "main", "1"), "user", 1,
            None, "complete", "合成：服务桥接里的白鹭"))
        async with Client(self.parameters, read_timeout_seconds=15) as mcp:
            names = [tool.name for tool in (await mcp.list_tools()).tools]
            self.assertIn("evernook_v1_capture_chunk", names)
            saved = await mcp.call_tool("evernook_v1_remember", {"event": event})
            self.assertFalse(saved.is_error, saved)
            assert saved.structured_content is not None
            message_id = saved.structured_content["result"]["message_id"]
            self.assertEqual(self.client.call(self.access, "read", {"message_id": message_id})["event"], event)
            managed = await asyncio.to_thread(subprocess.run, [sys.executable, "-B", "-m", "evernook.cli",
                "manage", "--config", str(self.config), "--access", str(self.owner_path)],
                input=json.dumps({"action": "read", "view": "status"}).encode(),
                capture_output=True, timeout=20)
            self.assertEqual(managed.returncode, 0, managed.stdout)
            denied = self.client.call(self.access, "cli", {"command": "manage",
                "input": {"action": "read", "view": "status"}})
            self.assertIsInstance(denied, dict)
            self.stop()
            failed = await mcp.call_tool("evernook_v1_remember", {"event": {**event,
                "source": {**event["source"], "event": "2"}}})
            self.assertTrue(failed.is_error)
        self.assertIsNotNone(self.service.poll())

    async def test_real_stdio_capture_with_broken_optional_model_configs(self) -> None:
        from evernook.models import MemoryFailure

        self.stop()
        endpoint = self.config.parent / 'memory-service.json'
        previous = endpoint.read_bytes()
        embedding = self.config.parent.parent / 'synthetic-embedding.json'
        context = self.config.parent.parent / 'synthetic-context.json'
        embedding.write_text('{', encoding='utf-8')
        context.write_text('{', encoding='utf-8')
        self.service = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--embedding-config', str(embedding), '--context-config', str(context)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while not endpoint.exists() or endpoint.read_bytes() == previous:
            if self.service.poll() is not None or time.monotonic() > deadline:
                self.fail('isolated activated service startup failed')
            await asyncio.sleep(0.05)
        parameters = StdioServerParameters(command=sys.executable, args=[*self.parameters.args, '--require-service',
            '--embedding-config', str(embedding), '--context-config', str(context)], env=dict(os.environ))
        ordinary = self.client.call(self.access, 'status', {})
        self.assertTrue(ordinary['service']['single_writer'])
        with self.assertRaisesRegex(MemoryFailure, 'service_policy_mismatch'):
            self.client.call(self.access, 'policy-match', {'embedding_config': None, 'context_config': None})
        with self.assertRaisesRegex(MemoryFailure, 'operation_not_allowed'):
            self.client.call(self.access, 'mcp', {'profile': 'recall', 'name': 'evernook_v1_capture', 'arguments': {}})
        event = asdict(Event(Source('test', 'synthetic-bridge', 'main', '1'), 'user', 1, None,
                             'complete', '合成：配置错误时仍保存的对话'))
        async with Client(parameters, read_timeout_seconds=20) as mcp:
            names = [tool.name for tool in (await mcp.list_tools()).tools]
            self.assertIn('evernook_v1_capture', names)
            status = await mcp.call_tool('evernook_v1_status', {})
            self.assertFalse(status.is_error, status)
            assert status.structured_content is not None
            self.assertEqual(status.structured_content['result']['semantic']['state'], 'unavailable')
            self.assertEqual(status.structured_content['result']['context']['available'], False)
            result = await mcp.call_tool('evernook_v1_capture', {'event': event, 'kind': 'message'})
            self.assertFalse(result.is_error, result)
            assert result.structured_content is not None
            message_id = result.structured_content['result']['message']['message_id']
            self.assertEqual(self.client.call(self.access, 'read', {'message_id': message_id})['event'], event)
            recalled = await mcp.call_tool('evernook_v1_recall', {'query': '合成'})
            self.assertTrue(recalled.is_error)
            assert recalled.structured_content is not None
            self.assertEqual(recalled.structured_content['error']['code'], 'invalid_config')
            context_result = await mcp.call_tool('evernook_v1_context', {'query': '合成', 'model_id': 'synthetic',
                'max_tokens': 512, 'as_of': '2024-01-01T00:00:00Z'})
            self.assertTrue(context_result.is_error)

    async def test_ordinary_identity_cannot_use_management_or_recall_profile_writes(self) -> None:
        ordinary = json.loads(self.ordinary_path.read_text(encoding="utf-8"))
        from evernook.models import Access, MemoryFailure
        with self.assertRaises(MemoryFailure):
            self.client.call(Access(**ordinary), "cli", {"command": "manage",
                "input": {"action": "preview", "operation": "source_suppress", "message_id": "absent"}})
        with self.assertRaisesRegex(MemoryFailure, "operation_not_allowed"):
            self.client.call(self.access, "mcp", {"name": "evernook_v1_remember",
                "arguments": {}, "profile": "recall"})
        with self.assertRaisesRegex(MemoryFailure, "operation_not_allowed"):
            self.client.call(self.access, "cli", {"command": "pair", "input": {}})

    async def test_disconnected_caller_cancels_cooperative_service_operation(self) -> None:
        from evernook.models import MemoryFailure
        entered, canceled, caller_cancel = threading.Event(), threading.Event(), threading.Event()
        def cooperative(runtime: MemoryRuntime, request: dict[str, object], *,
                        cancel: threading.Event | None = None, **kwargs: object) -> object:
            entered.set()
            assert cancel is not None
            if cancel.wait(5):
                canceled.set()
            return {}
        with _Server(MemoryRuntime(self.config), b"x" * 32) as server:
            endpoint = self.config.parent / "memory-service.json"
            original = endpoint.read_bytes()
            endpoint.write_text(json.dumps({"version": 1, "port": server.server_address[1],
                "key": (b"x" * 32).hex()}), encoding="utf-8")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with patch("evernook.service.dispatch", cooperative):
                    pending = asyncio.create_task(asyncio.to_thread(self.client.call,
                        self.access, "status", {}, cancel=caller_cancel))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    caller_cancel.set()
                    with self.assertRaisesRegex(MemoryFailure, "service_canceled"):
                        await pending
                    self.assertTrue(await asyncio.to_thread(canceled.wait, 5))
            finally:
                server.shutdown()
                thread.join(timeout=10)
                endpoint.write_bytes(original)

    async def test_real_context_worker_and_cancellation_through_service(self) -> None:
        from evernook.models import MemoryFailure
        fixture = test_tokenizer.LocalTokenizerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context_file = fixture.root / "service-context.json"
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(fixture.config),
            "policy": {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 0,
                "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
        capture_only = pair_client(self.config, client_id="capture-only", entry="capture-only",
            access_path=self.config.parent / "mcp-access-capture.json", scopes=("capture",))
        arguments: dict[str, Any] = {"name": "evernook_v1_context", "profile": "trusted-host", "arguments": {
            "query": "hello", "model_id": "synthetic-local", "max_tokens": 256,
            "as_of": "2026-11-04T00:00:00Z", "mode": "normal", "timeout": 5}}
        with _Server(MemoryRuntime(self.config), b"x" * 32, context_config=context_file) as server:
            endpoint = self.config.parent / "memory-service.json"
            original = endpoint.read_bytes()
            endpoint.write_text(json.dumps({"version": 1, "port": server.server_address[1],
                "key": (b"x" * 32).hex()}), encoding="utf-8")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                result = await asyncio.to_thread(self.client.call, self.access, "mcp", arguments)
                from evernook.tokenizer import LocalTokenCounter
                self.assertEqual(result["counter_id"], LocalTokenCounter.from_file(fixture.config).counter_id)
                bundle = await asyncio.to_thread(self.client.context, self.access, "hello",
                    model_id="synthetic-local", max_tokens=256, as_of="2026-11-04T00:00:00Z", timeout=5)
                self.assertEqual(bundle, result)
                command = [sys.executable, "-B", "-m", "evernook.cli", "context",
                    "--config", str(self.config), "--access", str(self.owner_path), "--require-service"]
                cli = await asyncio.to_thread(subprocess.run, command,
                    input=json.dumps(arguments["arguments"]).encode(), capture_output=True, timeout=30)
                self.assertEqual(cli.returncode, 0, cli.stdout.decode("utf-8", errors="replace"))
                self.assertEqual(json.loads(cli.stdout)["result"], result)
                with self.assertRaises(MemoryFailure):
                    await asyncio.to_thread(self.client.context, capture_only, "hello",
                        model_id="synthetic-local", max_tokens=256, as_of="2026-11-04T00:00:00Z", timeout=5)
                with self.assertRaises(MemoryFailure):
                    await asyncio.to_thread(self.client.call, self.access, "cli",
                        {"command": "context", "input": {**arguments["arguments"],
                            "context_config": str(context_file)}})
                canceled = threading.Event()
                spawned = threading.Event()
                children: list[subprocess.Popen[str]] = []
                real_popen = subprocess.Popen
                def hang(command: Any, **options: Any) -> subprocess.Popen[str]:
                    if Path(command[0]).name.lower() != "node.exe":
                        return real_popen(command, **options)
                    child = real_popen([str(command[0]), "-e",
                        "process.stdin.resume();setInterval(()=>{},1000)"], **options)
                    children.append(child)
                    spawned.set()
                    return child
                with patch("evernook.tokenizer.subprocess.Popen", side_effect=hang):
                    pending = asyncio.create_task(asyncio.to_thread(self.client.call,
                        self.access, "mcp", arguments, cancel=canceled))
                    self.assertTrue(await asyncio.to_thread(spawned.wait, 5))
                    canceled.set()
                    with self.assertRaisesRegex(MemoryFailure, "service_canceled"):
                        await pending
                    for child in children:
                        await asyncio.to_thread(child.wait, timeout=5)
                        self.assertIsNotNone(child.poll())
            finally:
                server.shutdown()
                thread.join(timeout=10)
                endpoint.write_bytes(original)

    async def test_context_fails_closed_without_service_configuration(self) -> None:
        from evernook.models import MemoryFailure
        with self.assertRaises(MemoryFailure):
            await asyncio.to_thread(self.client.context, self.access, "hello",
                model_id="synthetic-local", max_tokens=256, as_of="2026-11-04T00:00:00Z", timeout=5)
