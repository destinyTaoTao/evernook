from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from nacl import pwhash
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from evernook.models import MemoryFailure
from evernook.protected import initialize_protected_inbox
from evernook.service import ServiceClient


class ProtectedServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-protected-service-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path)
        self.reader_path = self.config.parent / 'mcp-access-reader.json'
        self.reader = pair_client(self.config, client_id='reader', entry='reader', access_path=self.reader_path,
                                  scopes=('recall',))
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys',
                                               b'synthetic service password')
        self.event = Event(Source('owner', 'synthetic-private-session', 'main', '1'), 'user', 1, None,
                           'complete', 'synthetic-private-sentinel-154', sensitive=True)
        self.process: subprocess.Popen[bytes] | None = None
        self.diagnostics: list[bytes] = []
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            if self.process.stderr:
                self.diagnostics.append(self.process.stderr.read())
                self.process.stderr.close()
            self.process = None

    def start(self, *, configured: bool, activate: bool = True) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        old = endpoint.read_bytes() if endpoint.exists() else None
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config)]
        if activate:
            args += ['--activate-owner', str(self.owner_path)]
        if configured:
            args += ['--protected-inbox', str(self.root / 'inbox'), '--protected-keys', str(self.root / 'keys')]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 20
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if self.process.poll() is not None:
                diagnostic = self.process.stderr.read().decode('utf-8', errors='replace') if self.process.stderr else ''
                self.fail('protected service exited: ' + diagnostic)
            if time.monotonic() > deadline:
                self.fail('protected service did not become ready')
            time.sleep(0.05)
        return ServiceClient(self.config)

    def test_explicit_service_capture_encrypts_and_never_returns_plaintext(self) -> None:
        client = self.start(configured=True)
        self.assertTrue(client.call(self.owner, 'status', {})['service']['protected_capture'])
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.reader, 'protected-receive', {'event': asdict(self.event)})
        other = asdict(self.event)
        other['source']['entry'] = 'reader'
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            client.call(self.owner, 'protected-receive', {'event': other})
        unmarked = asdict(self.event)
        unmarked['sensitive'] = False
        with self.assertRaisesRegex(MemoryFailure, 'protected_explicit_mark_required'):
            client.call(self.owner, 'protected-receive', {'event': unmarked})
        incomplete = asdict(self.event)
        incomplete['completion'] = 'partial'
        with self.assertRaisesRegex(MemoryFailure, 'protected_incomplete_unsupported'):
            client.call(self.owner, 'protected-receive', {'event': incomplete})
        first = client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        again = client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        self.assertEqual(first, again)
        self.assertEqual(first['state'], 'protected_linked')
        self.assertTrue(first['encrypted_original_saved'])
        self.assertFalse(first['message']['original_saved'])
        self.assertNotIn('synthetic-private-sentinel-154', json.dumps(first))
        tools = client.call(self.owner, 'mcp-list', {'profile': 'trusted-host'})['tools']
        self.assertNotIn('protected-receive', [item['name'] for item in tools])
        read = client.call(self.owner, 'read', {'message_id': first['message']['message_id']})
        self.assertNotIn('synthetic-private-sentinel-154', json.dumps(read))
        self.assertEqual(client.call(self.owner, 'search', {'query': 'synthetic-private-sentinel-154'})['hits'], [])
        cli_event = asdict(self.event)
        cli_event['source']['event'] = 'cli'
        cli_event['text'] = 'password=synthetic-vault-password-154'
        cli_result = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'service-protected-receive',
            '--config', str(self.config), '--access', str(self.owner_path)],
            input=json.dumps(cli_event).encode('utf-8'), capture_output=True, timeout=30)
        self.assertEqual(cli_result.returncode, 0, cli_result.stdout)
        self.assertEqual(json.loads(cli_result.stdout)['result']['state'], 'protected_linked')
        self.assertNotIn(b'synthetic-vault-password-154', cli_result.stdout + cli_result.stderr)
        self.stop()
        client = self.start(configured=True, activate=False)
        self.assertEqual(client.call(self.owner, 'protected-receive', {'event': asdict(self.event)}), first)
        self.stop()
        recovery = json.loads((self.root / 'keys/recovery.json').read_text(encoding='utf-8'))
        derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, b'synthetic service password',
            bytes.fromhex(recovery['salt']), opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE,
            memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        private = json.loads(SecretBox(derived).decrypt(bytes.fromhex(recovery['ciphertext'])))
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            envelope = json.loads(db.execute('SELECT envelope FROM objects WHERE id=?', (first['object_id'],)).fetchone()[0])
        clear = json.loads(SealedBox(PrivateKey(bytes.fromhex(private['private']))).decrypt(bytes.fromhex(envelope['ciphertext'])))
        self.assertEqual(clear['event'], asdict(self.event))
        contents = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(b'synthetic-private-sentinel-154', contents)
        self.assertNotIn(b'synthetic-vault-password-154', contents)
        self.assertNotIn(b'synthetic-private-sentinel-154', b''.join(self.diagnostics))

    def test_unconfigured_service_refuses_protected_capture(self) -> None:
        client = self.start(configured=False)
        self.assertFalse(client.call(self.owner, 'status', {})['service']['protected_capture'])
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM objects').fetchone()[0], 0)

    def test_activation_refuses_existing_pending_inbox(self) -> None:
        self.inbox.stage(self.owner, self.event)
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'keys')], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertEqual(len(self.inbox.pending(self.owner)), 1)

    def test_incomplete_service_configuration_refuses_before_activation(self) -> None:
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox')],
            capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid_protected_service_config', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertFalse((self.config.parent / 'memory-service.json').exists())

    def test_mismatched_inbox_material_refuses_before_activation(self) -> None:
        other_config, _ = initialize(self.root / 'other-ordinary', client_id='other', entry='other')
        initialize_protected_inbox(MemoryRuntime(other_config), self.root / 'other-inbox',
                                   self.root / 'other-keys', b'synthetic other password')
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'other-keys')], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_binding_mismatch', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertFalse((self.config.parent / 'memory-service.json').exists())

    def test_missing_writer_material_fails_without_plaintext_fallback(self) -> None:
        client = self.start(configured=True)
        (self.root / 'keys/writer.key').unlink()
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_material'):
            client.protected_receive(self.owner, self.event)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)
        self.stop()
        contents = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(b'synthetic-private-sentinel-154', contents)

    def test_failed_core_link_survives_restart_and_retry(self) -> None:
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3', isolation_level=None)) as db:
            db.execute('CREATE TABLE protected_links_v1 (message_id TEXT PRIMARY KEY REFERENCES messages(id), reference TEXT NOT NULL)')
            db.execute("CREATE TRIGGER synthetic_link_failure BEFORE INSERT ON protected_links_v1 BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        client = self.start(configured=True)
        with self.assertRaises(MemoryFailure):
            client.protected_receive(self.owner, self.event)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)
        self.stop()
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM objects WHERE state='protected_pending_link'").fetchone()[0], 1)
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3', isolation_level=None)) as db:
            db.execute('DROP TRIGGER synthetic_link_failure')
        client = self.start(configured=True, activate=False)
        self.assertEqual(client.protected_receive(self.owner, self.event)['state'], 'protected_linked')
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 1)

    def test_staging_holds_cutover_gate_until_pending_object_is_visible(self) -> None:
        ready, release = threading.Event(), threading.Event()
        original = self.inbox._stage_authorized
        def paused(entry: str, event: Event):  # type: ignore[no-untyped-def]
            ready.set()
            if not release.wait(15):
                raise RuntimeError('synthetic race timeout')
            return original(entry, event)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(self.inbox, '_stage_authorized', side_effect=paused):
                staged = pool.submit(self.inbox.stage, self.owner, self.event)
                self.assertTrue(ready.wait(10))
                process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                    '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
                    '--protected-keys', str(self.root / 'keys')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    try:
                        time.sleep(2)
                        self.assertIsNone(process.poll())
                        self.assertFalse((self.config.parent / 'memory-service.json').exists())
                    finally:
                        release.set()
                    self.assertEqual(staged.result(timeout=15).state, 'protected_pending_link')
                    _, stderr = process.communicate(timeout=20)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(b'protected_cutover_requires_migration', stderr)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=15)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
