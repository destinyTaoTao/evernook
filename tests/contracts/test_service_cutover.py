from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch
from typing import Any

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from evernook.models import MemoryFailure
from evernook.service import ServiceClient


class ServiceCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-service-cutover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.access_path = self.config.parent / "mcp-access-owner.json"
        self.access_path.write_text(json.dumps(asdict(self.access)), encoding="utf-8")
        self.event = Event(Source("test", "cutover", "main", "1"), "user", 1,
            None, "complete", "合成：切换前的白鹭原话")
        self.receipt = self.runtime.submit(self.access, self.event)
        self.process: subprocess.Popen[bytes] | None = None
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            if self.process.stderr:
                self.process.stderr.close()

    def start(self) -> None:
        endpoint = self.config.parent / "memory-service.json"
        old = endpoint.read_bytes() if endpoint.exists() else None
        self.process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config), "--activate-owner", str(self.access_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.fail("service cutover startup failed")
            time.sleep(0.05)

    def test_cutover_blocks_old_local_runtime_and_restarts_without_data_loss(self) -> None:
        self.start()
        with self.assertRaisesRegex(MemoryFailure, "service_required"):
            MemoryRuntime(self.config)
        with self.assertRaisesRegex(MemoryFailure, "service_required"):
            self.runtime.submit(self.access, self.event)
        client = ServiceClient(self.config)
        self.assertEqual(client.call(self.access, "read", {"message_id": self.receipt.message_id})["event"],
            asdict(self.event))
        read = subprocess.run([sys.executable, "-B", "-m", "evernook.cli", "read",
            "--config", str(self.config), "--access", str(self.access_path),
            "--message-id", self.receipt.message_id], capture_output=True, timeout=15)
        self.assertEqual(read.returncode, 0, read.stdout)
        newer = asdict(Event(Source("test", "cutover", "main", "2"), "user", 2,
            None, "complete", "合成：切换后新写入"))
        saved = client.call(self.access, "remember", {"event": newer})
        self.stop()
        self.start()
        self.assertEqual(client.call(self.access, "read", {"message_id": saved["message_id"]})["event"], newer)

    def test_lost_discovery_does_not_reenable_ordinary_cli_writes(self) -> None:
        self.start()
        self.stop()
        (self.config.parent / "memory-service.json").unlink()
        result = subprocess.run([sys.executable, "-B", "-m", "evernook.cli", "remember",
            "--config", str(self.config), "--access", str(self.access_path)],
            input=json.dumps(asdict(Event(Source("test", "cutover", "main", "2"),
                "user", 2, None, "complete", "合成：禁止离线直写"))).encode(),
            capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_write_authorized_before_activation_cannot_cross_cutover(self) -> None:
        ready, release = threading.Event(), threading.Event()
        original = self.runtime._require_scope
        def authorized(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            ready.set()
            if not release.wait(15):
                self.fail("cutover barrier timeout")
            return result
        later = Event(Source("test", "cutover", "main", "2"), "user", 2, None,
            "complete", "合成：授权后延迟的旧写入")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(self.runtime, "_require_scope", side_effect=authorized):
                future = executor.submit(self.runtime.submit, self.access, later)
                try:
                    self.assertTrue(ready.wait(5))
                    self.start()
                finally:
                    release.set()
                with self.assertRaisesRegex(MemoryFailure, "service_required"):
                    future.result(timeout=10)
        self.assertEqual(ServiceClient(self.config).call(self.access, "status", {})["ledger"]["messages"], 1)

    def test_service_pairing_can_recover_after_lockout_with_admin(self) -> None:
        from evernook.service import client_access
        self.start()
        client = ServiceClient(self.config)
        client.call(self.access, "pair-configure", {"failure_limit": 1})
        with self.assertRaises(MemoryFailure):
            client.call(self.access, "pair-client", {"client_id": "conflict", "entry": "test", "scopes": None})
        with self.assertRaisesRegex(MemoryFailure, "pairing_locked"):
            client.call(self.access, "pair-client", {"client_id": "new-client", "entry": "new-entry", "scopes": None})
        client.call(self.access, "pair-unlock", {})
        paired = client.call(self.access, "pair-client", {
            "client_id": "new-client", "entry": "new-entry", "scopes": None})
        access = client_access(self.config, Path(paired["access_file"]))
        self.assertEqual(client.call(access, "status", {})["capabilities"]["scopes"], ["capture", "recall"])
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            client.call(access, "pair-unlock", {})

    def test_activation_requires_admin_and_preserves_existing_protected_assets(self) -> None:
        ordinary = self.config.parent / "mcp-access-ordinary.json"
        pair_client(self.config, client_id="ordinary", entry="ordinary", access_path=ordinary,
            scopes=("capture", "recall"))
        denied = subprocess.run([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config), "--activate-owner", str(ordinary)],
            capture_output=True, timeout=15)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(b"permission_denied", denied.stderr)
        self.assertNotIn("service_mode", json.loads(self.config.read_text(encoding="utf-8")))
        from evernook.protected import initialize_protected_inbox
        from evernook.vault_local import LocalDisplayRequest
        password = b"synthetic-cutover-secret"
        inbox = initialize_protected_inbox(self.runtime, self.root / "inbox", self.root / "keys", password)
        secret = Event(Source("test", "cutover-vault", "main", "1"), "user", 1, None,
            "complete", "password=synthetic-private-sentinel")
        receipt = inbox.submit(self.access, secret)
        refused = subprocess.run([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config), "--activate-owner", str(self.access_path)],
            capture_output=True, timeout=15)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b"protected_cutover_requires_migration", refused.stderr)
        self.assertNotIn("service_mode", json.loads(self.config.read_text(encoding="utf-8")))
        displayed: list[str] = []
        LocalDisplayRequest(inbox, self.access, receipt.object_id, "合成切换保全",
            desktop=lambda: True).display_once(password, displayed.append)
        self.assertEqual(displayed, [secret.text])
        stored = self.runtime.read(self.access, self.receipt.message_id)
        assert stored is not None
        self.assertEqual(stored.event, self.event)
