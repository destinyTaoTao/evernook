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

    def test_unactivated_start_requires_explicit_choice(self) -> None:
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service',
            '--config', str(self.config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            try:
                _, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=15)
                self.fail('unactivated service started without an explicit mode')
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b'service_activation_required', stderr)
            self.assertFalse((self.config.parent / 'memory-service.json').exists())
            self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)

    def test_explicit_legacy_mode_reports_shared_writers(self) -> None:
        endpoint = self.config.parent / 'memory-service.json'
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service',
            '--config', str(self.config), '--allow-legacy-shared'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('explicit legacy service did not start')
                time.sleep(0.05)
            client = ServiceClient(self.config)
            self.assertFalse(client.call(self.access, 'status', {})['service']['single_writer'])
            event = Event(Source('test', 'legacy-check', 'main', '2'), 'user', 2,
                None, 'complete', 'synthetic legacy write')
            self.assertEqual(self.runtime.submit(self.access, event).status, 'committed')
            self.assertEqual(client.call(self.access, 'status', {})['ledger']['messages'], 2)
        finally:
            if process.poll() is None:
                process.terminate()
            _, stderr = process.communicate(timeout=15)
        self.assertIn(b'legacy_shared_mode_not_single_writer', stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_activation_and_legacy_mode_are_mutually_exclusive(self) -> None:
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service',
            '--config', str(self.config), '--activate-owner', str(self.access_path), '--allow-legacy-shared'],
            capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid_service_mode', result.stderr)
        self.assertFalse((self.config.parent / 'memory-service.json').exists())
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def start(self, *, activate: bool = True) -> None:
        endpoint = self.config.parent / "memory-service.json"
        old = endpoint.read_bytes() if endpoint.exists() else None
        command = [sys.executable, "-B", "-m", "evernook.service", "--config", str(self.config)]
        if activate:
            command.extend(["--activate-owner", str(self.access_path)])
        self.process = subprocess.Popen(command,
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
        self.assertTrue(client.call(self.access, 'status', {})['service']['single_writer'])
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
        self.start(activate=False)
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

    def test_pending_reconcile_cannot_cross_actual_service_activation(self) -> None:
        from evernook.protected import initialize_protected_inbox
        inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys', b'synthetic-gate-test-password')
        pending = inbox.stage(self.access, Event(Source('test', 'synthetic-vault', 'main', 'pending'),
            'user', 2, None, 'complete', 'synthetic-pending-cutover-secret'))
        authorized, release = threading.Event(), threading.Event()
        original = self.runtime._require_scope
        def pause(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            authorized.set()
            if not release.wait(20):
                raise RuntimeError('synthetic cutover test timed out')
            return value
        with patch.object(self.runtime, '_require_scope', side_effect=pause), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(inbox.reconcile, self.access, pending.object_id)
            try:
                self.assertTrue(authorized.wait(10))
                endpoint = self.config.parent / 'memory-service.json'
                # An unbound external inbox is currently undiscoverable; a future
                # fail-closed activation is also acceptable, not a required startup.
                self.process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                    '--activate-owner', str(self.access_path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                deadline = time.monotonic() + 15
                while not endpoint.exists() and self.process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                activated = endpoint.exists() and self.process.poll() is None
                if not activated:
                    self.assertIsNotNone(self.process.poll(), 'service startup timed out')
                    assert self.process.stderr is not None
                    self.assertIn(b'protected_cutover_requires_migration', self.process.stderr.read())
            finally:
                release.set()
            if activated:
                with self.assertRaisesRegex(MemoryFailure, 'service_required'):
                    future.result(timeout=15)
                with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT state FROM objects WHERE id=?', (pending.object_id,)).fetchone()[0], 'protected_pending_link')
            else:
                self.assertEqual(future.result(timeout=15).state, 'protected_linked')

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
        staging = subprocess.run([sys.executable, '-B', '-m', 'evernook.service',
            '--config', str(self.config), '--allow-legacy-shared'], capture_output=True, timeout=15)
        self.assertNotEqual(staging.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', staging.stderr)
        self.assertFalse((self.config.parent / 'memory-service.json').exists())
        displayed: list[str] = []
        LocalDisplayRequest(inbox, self.access, receipt.object_id, "合成切换保全",
            desktop=lambda: True).display_once(password, displayed.append)
        self.assertEqual(displayed, [secret.text])
        stored = self.runtime.read(self.access, self.receipt.message_id)
        assert stored is not None
        self.assertEqual(stored.event, self.event)
