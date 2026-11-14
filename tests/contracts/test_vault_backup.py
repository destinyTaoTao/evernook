from __future__ import annotations

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evernook import Capture, Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.backup import _decrypt, _unlock, initialize_backup_keys
from evernook.paths import provision_private_directory
from evernook.protected import ProtectedInbox, initialize_protected_inbox
from evernook.runtime import _json
from evernook.service import ServiceClient
from evernook.vault_crypto import _decrypt_event, _unlock as unlock_vault


class VaultBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-vault-backup-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.owner_file = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_file)
        self.reader = pair_client(self.config, client_id='reader', entry='reader',
            access_path=self.config.parent / 'mcp-access-reader.json', scopes=('capture', 'recall'))
        self.vault_password = b'synthetic-vault-password-213'
        self.backup_password = b'synthetic-backup-password-213'
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'vault-keys', self.vault_password)
        self.keys = initialize_backup_keys(self.root / 'backup-keys', self.backup_password)
        self.event = Event(Source('owner', 'synthetic', 'main', 'one'), 'user', 1, None, 'complete',
                           'synthetic-vault-secret-213', sensitive=True)
        self.process: subprocess.Popen[bytes] | None = None
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.communicate(timeout=20)
            self.process = None

    def start(self, *, configured: bool = True) -> ServiceClient:
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                '--activate-owner', str(self.owner_file)]
        if configured:
            args.extend(['--protected-inbox', str(self.root / 'inbox'),
                         '--protected-keys', str(self.root / 'vault-keys')])
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while not (self.config.parent / 'memory-service.json').exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.fail('synthetic service failed to start')
            time.sleep(.05)
        return ServiceClient(self.config)

    def test_service_package_is_encrypted_bound_and_independently_decryptable(self) -> None:
        client = self.start()
        received = client.protected_receive(self.owner, self.event)
        stream_event = replace(self.event, source=replace(self.event.source, event='stream'), text='')
        stream = client.protected_capture(self.owner, Capture(stream_event, 'message', text_chunks=('合成', 'stream-224')))
        self.assertEqual(stream['status'], 'saved')
        destination = self.root / 'encrypted-package'
        status = client.protected_backup(self.owner, destination, self.root / 'backup-keys')
        self.assertEqual(status['protected_objects'], 2)
        self.assertEqual(status['verification'], 'signature_and_links_only')
        self.assertFalse(status['recovery_tested'])
        self.assertNotIn(self.event.text, json.dumps(status))
        files = b''.join(path.read_bytes() for path in destination.iterdir())
        self.assertNotIn(self.event.text.encode(), files)
        self.assertNotIn(self.vault_password, files)
        self.assertNotIn((self.root / 'vault-keys' / 'writer.key').read_bytes(), files)
        key_id, private, signer = _unlock(Path(self.keys.recovery_file), self.backup_password)
        header = json.loads((destination / 'backup.json').read_text(encoding='utf-8'))
        content = header['content']
        signer.verify(_json(content).encode(), bytes.fromhex(header['signature']))
        self.assertEqual(content['status']['backup_id'], status['backup_id'])
        decoded: dict[str, bytes] = {}
        for name, info in content['files'].items():
            output = io.BytesIO()
            aad = _json({'backup_id': status['backup_id'], 'backup_key_id': key_id, 'file': name}).encode()
            _decrypt(destination / (name + '.enc'), output, private, info, aad)
            decoded[name] = output.getvalue()
        self.assertEqual(set(decoded), {'binding', 'receiving', 'writer', 'recovery', 'inbox'})
        binding = json.loads(decoded['binding'])
        self.assertEqual(binding['objects'], 2)
        self.assertEqual(len(binding['link_digest']), 64)
        self.assertEqual(decoded['recovery'], (self.root / 'vault-keys' / 'recovery.json').read_bytes())
        # Reconstruct only a disposable synthetic copy and prove the second, separate vault password works.
        restored_inbox, restored_keys = self.root / 'copy-inbox', self.root / 'copy-keys'
        provision_private_directory(restored_inbox)
        provision_private_directory(restored_keys)
        (restored_inbox / 'inbox.sqlite3').write_bytes(decoded['inbox'])
        for name in ('receiving', 'writer', 'recovery'):
            (restored_keys / ('writer.key' if name == 'writer' else name + '.json')).write_bytes(decoded[name])
        service_runtime = MemoryRuntime(self.config, service_owner=True)
        copied = ProtectedInbox(service_runtime, restored_inbox, restored_keys)
        vault_private = unlock_vault(copied, self.vault_password)
        with copied._connection() as db:
            row = db.execute('SELECT * FROM objects WHERE id=?', (received['object_id'],)).fetchone()
            self.assertEqual(_decrypt_event(copied, row, vault_private), self.event)
            stream_row = db.execute('SELECT * FROM objects WHERE id=?', (stream['object_id'],)).fetchone()
            value, _ = copied._verified(stream_row)
            self.assertEqual(value['bound']['version'], 2)
            self.assertEqual(_decrypt_event(copied, stream_row, vault_private).text, '合成stream-224')

    def test_unconfigured_service_refuses_backup(self) -> None:
        client = self.start(configured=False)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.protected_backup(self.owner, self.root / 'unconfigured', self.root / 'backup-keys')
        self.assertFalse((self.root / 'unconfigured').exists())

    def test_scope_path_and_cancellation_fail_closed(self) -> None:
        client = self.start()
        client.protected_receive(self.owner, self.event)
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.protected_backup(self.reader, self.root / 'denied', self.root / 'backup-keys')
        with self.assertRaisesRegex(MemoryFailure, 'unsafe_backup_path'):
            client.protected_backup(self.owner, self.root / 'inbox' / 'nested', self.root / 'backup-keys')
        stopped = threading.Event(); stopped.set()
        with self.assertRaises(MemoryFailure):
            client.protected_backup(self.owner, self.root / 'canceled', self.root / 'backup-keys', cancel=stopped)
        self.assertFalse((self.root / 'denied').exists())
        self.assertFalse((self.root / 'canceled').exists())

    def test_pre_canceled_sdk_never_sends_request(self) -> None:
        client = ServiceClient(self.config)
        canceled = threading.Event(); canceled.set()
        with patch.object(client, 'call') as call:
            with self.assertRaisesRegex(MemoryFailure, 'service_canceled'):
                client.protected_backup(self.owner, self.root / 'canceled', self.root / 'backup-keys', cancel=canceled)
            call.assert_not_called()

    def test_cli_uses_bound_service_and_returns_only_public_receipt(self) -> None:
        client = self.start()
        client.protected_receive(self.owner, self.event)
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-protected-backup',
            '--config', str(self.config), '--access', str(self.owner_file), '--destination', str(self.root / 'cli-backup'),
            '--backup-keys', str(self.root / 'backup-keys')], capture_output=True, timeout=150)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.event.text.encode(), result.stdout + result.stderr)
        self.assertNotIn(self.vault_password, result.stdout + result.stderr)
        self.assertTrue((self.root / 'cli-backup' / 'backup.json').is_file())

    def test_cancellation_mid_encryption_does_not_publish(self) -> None:
        from evernook.vault_backup import create_vault_backup
        import evernook.vault_backup as module
        self.inbox.submit(self.owner, self.event)
        canceled = threading.Event()
        original = getattr(module, '_encrypt')
        def interrupted(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            canceled.set()
            return result
        with patch.object(module, '_encrypt', side_effect=interrupted):
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_canceled'):
                create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'canceled', self.root / 'backup-keys', cancel=canceled)
        self.assertFalse((self.root / 'canceled').exists())
        self.assertEqual(list(self.root.glob('.evernook-vault-backup-*')), [])

    def test_backup_gate_excludes_concurrent_private_write(self) -> None:
        from evernook.vault_backup import create_vault_backup
        import evernook.vault_backup as module
        self.inbox.submit(self.owner, self.event)
        entered, release = threading.Event(), threading.Event()
        original = getattr(module, '_encrypt')
        def blocked(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(30):
                raise RuntimeError('synthetic coordination timeout')
            return original(*args, **kwargs)
        later = replace(self.event, source=replace(self.event.source, event='two'), text='synthetic-later-secret-213')
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                with patch.object(module, '_encrypt', side_effect=blocked):
                    backup = pool.submit(create_vault_backup, self.runtime, self.inbox, self.owner,
                                         self.root / 'concurrent', self.root / 'backup-keys')
                    self.assertTrue(entered.wait(30))
                    writer = pool.submit(self.inbox.submit, self.owner, later)
                    time.sleep(.2)
                    self.assertFalse(writer.done())
                    release.set()
                    self.assertEqual(backup.result(timeout=30).protected_objects, 1)
                    self.assertEqual(writer.result(timeout=30).state, 'protected_linked')
            finally:
                release.set()

    def test_pending_source_rejects_backup_without_publishing(self) -> None:
        pending = self.inbox.stage(self.owner, self.event)
        from evernook.vault_backup import create_vault_backup
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'pending-backup', self.root / 'backup-keys')
        self.assertFalse((self.root / 'pending-backup').exists())
        self.assertEqual(self.inbox.status(self.owner, pending.object_id).state, 'protected_pending_link')

    def test_empty_registered_inbox_is_supported_and_existing_destination_is_preserved(self) -> None:
        from evernook.vault_backup import create_vault_backup
        self.inbox.register(self.owner)
        destination = self.root / 'empty-backup'
        status = create_vault_backup(self.runtime, self.inbox, self.owner, destination, self.root / 'backup-keys')
        self.assertEqual(status.protected_objects, 0)
        original = (destination / 'backup.json').read_bytes()
        with self.assertRaisesRegex(MemoryFailure, 'backup_destination_exists'):
            create_vault_backup(self.runtime, self.inbox, self.owner, destination, self.root / 'backup-keys')
        self.assertEqual((destination / 'backup.json').read_bytes(), original)

    def test_tampered_inbox_signature_is_rejected(self) -> None:
        from evernook.vault_backup import create_vault_backup
        receipt = self.inbox.submit(self.owner, self.event)
        with self.inbox._connection() as db:
            row = db.execute('SELECT envelope FROM objects WHERE id=?', (receipt.object_id,)).fetchone()
            value = json.loads(row['envelope'])
            value['signature'] = '00' * 64
            db.execute('UPDATE objects SET envelope=? WHERE id=?', (_json(value), receipt.object_id))
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'corrupt-backup', self.root / 'backup-keys')
        self.assertFalse((self.root / 'corrupt-backup').exists())

    def test_malformed_vault_recovery_material_is_rejected(self) -> None:
        from evernook.vault_backup import create_vault_backup
        self.inbox.submit(self.owner, self.event)
        (self.root / 'vault-keys' / 'recovery.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(MemoryFailure, 'invalid_vault_recovery'):
            create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'bad-recovery', self.root / 'backup-keys')
        self.assertFalse((self.root / 'bad-recovery').exists())

    def test_link_binding_is_stable_across_later_ordinary_suppression(self) -> None:
        from evernook.vault_backup import create_vault_backup
        receipt = self.inbox.submit(self.owner, self.event)
        assert receipt.message is not None
        first = self.root / 'first-backup'
        second = self.root / 'second-backup'
        create_vault_backup(self.runtime, self.inbox, self.owner, first, self.root / 'backup-keys')
        instruction = self.runtime.submit(self.owner, replace(self.event, source=replace(self.event.source, event='control'),
            text='合成：停止召回此记录', sensitive=False))
        self.runtime.change(self.owner, Change('synthetic-suppress', receipt.message.message_id, 1, 'suppress', instruction.message_id, 'synthetic'))
        create_vault_backup(self.runtime, self.inbox, self.owner, second, self.root / 'backup-keys')
        key_id, private, _ = _unlock(Path(self.keys.recovery_file), self.backup_password)
        digests = []
        for path in (first, second):
            header = json.loads((path / 'backup.json').read_text(encoding='utf-8'))['content']
            output = io.BytesIO()
            aad = _json({'backup_id': header['status']['backup_id'], 'backup_key_id': key_id, 'file': 'binding'}).encode()
            _decrypt(path / 'binding.enc', output, private, header['files']['binding'], aad)
            digests.append(json.loads(output.getvalue())['link_digest'])
        self.assertEqual(digests[0], digests[1])
        self.assertIsNone(self.runtime.read(self.owner, receipt.message.message_id))

    def test_crash_before_or_after_publication_keeps_source_and_never_overwrites(self) -> None:
        from evernook.vault_backup import create_vault_backup
        receipt = self.inbox.submit(self.owner, self.event)
        worker = Path(__file__).with_name('vault_backup_worker.py')
        for phase, code in [('before', 85), ('after', 86)]:
            destination = self.root / ('crash-' + phase)
            process = subprocess.run([sys.executable, str(worker), str(self.config), str(self.root / 'inbox'),
                str(self.root / 'vault-keys'), str(self.root / 'backup-keys'), str(destination), phase],
                capture_output=True, timeout=60)
            self.assertEqual(process.returncode, code, process.stderr)
            self.assertEqual(destination.exists(), phase == 'after')
            self.assertEqual(self.inbox.status(self.owner, receipt.object_id).state, 'protected_linked')
            if phase == 'before':
                create_vault_backup(self.runtime, self.inbox, self.owner, destination, self.root / 'backup-keys')
            else:
                before = (destination / 'backup.json').read_bytes()
                with self.assertRaisesRegex(MemoryFailure, 'backup_destination_exists'):
                    create_vault_backup(self.runtime, self.inbox, self.owner, destination, self.root / 'backup-keys')
                self.assertEqual((destination / 'backup.json').read_bytes(), before)
            key_id, private, sender = _unlock(Path(self.keys.recovery_file), self.backup_password)
            header = json.loads((destination / 'backup.json').read_text(encoding='utf-8'))
            content = header['content']
            sender.verify(_json(content).encode(), bytes.fromhex(header['signature']))
            for name, info in content['files'].items():
                output = io.BytesIO()
                aad = _json({'backup_id': content['status']['backup_id'], 'backup_key_id': key_id, 'file': name}).encode()
                _decrypt(destination / (name + '.enc'), output, private, info, aad)
