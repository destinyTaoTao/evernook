from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evernook import Access, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.backup import _decrypt, _encrypt, _unlock, _writer, create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import initialize_protected_inbox
from evernook.service import ServiceClient
from evernook.vault_backup import create_vault_backup
from evernook.vault_restore import VaultRestoreStage, restore_stage


class VaultRestoreStageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-vault-stage-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'original', client_id='synthetic-owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.password = b'synthetic-backup-password-212'
        self.keys = initialize_backup_keys(self.root / 'backup-keys', self.password)
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'vault', self.root / 'vault-keys', b'synthetic-vault-password-212')
        self.private = Event(Source('owner', 'private', 'main', 'one'), 'user', 1, None, 'complete',
                             'synthetic-private-sentinel-212', sensitive=True)
        self.inbox.submit(self.owner, self.private)
        create_backup(self.runtime, self.owner, self.root / 'backup', self.root / 'backup-keys')
        self.destination = self.root / 'restored'
        with OfflineRecovery(self.root / 'backup', self.destination, Path(self.keys.recovery_file), self.password) as review:
            review.publish(ACKNOWLEDGEMENT)
        self.new_owner = MemoryRuntime(self.destination / 'runtime.json').access_from_file(self.destination / 'mcp-access-owner.json')
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)

    def start(self, *flags: str) -> ServiceClient:
        endpoint = self.destination / 'memory-service.json'
        previous = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config',
            str(self.destination / 'runtime.json'), *flags], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        until = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == previous:
            if process.poll() is not None or time.monotonic() >= until:
                self.fail('synthetic service failed to start')
            time.sleep(.05)
        return ServiceClient(self.destination / 'runtime.json')
    def prepare_stage(self) -> None:
        create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'vault-backup', self.root / 'backup-keys')
        self.start('--activate-owner', str(self.destination / 'mcp-access-owner.json'), '--activate-restored-without-vault')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)

    def restore(self, destination: str = 'staged-vault', *, backup_password: bytes | None = None,
                vault_password: bytes = b'synthetic-vault-password-212', cancel: threading.Event | None = None) -> VaultRestoreStage:
        return restore_stage(self.destination / 'runtime.json', self.new_owner, self.root / 'vault-backup',
            self.root / destination, Path(self.keys.recovery_file), backup_password or self.password, vault_password, cancel=cancel)

    def test_matching_backup_restores_without_reattaching(self) -> None:
        self.prepare_stage()
        result = self.restore()
        self.assertEqual(result.protected_objects, 1)
        self.assertFalse(result.attached)
        self.assertFalse(result.daily_admitted)
        self.assertTrue((self.root / 'staged-vault' / 'inbox' / 'inbox.sqlite3').exists())
        self.assertNotIn(self.private.text.encode(), b''.join(p.read_bytes() for p in (self.root / 'staged-vault').rglob('*') if p.is_file()))
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_wrong_password_and_cancel_never_publish(self) -> None:
        self.prepare_stage()
        with self.assertRaises(MemoryFailure):
            self.restore(backup_password=b'synthetic-wrong-password')
        with self.assertRaises(MemoryFailure):
            self.restore(vault_password=b'synthetic-wrong-password')
        canceled = threading.Event()
        canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'vault_restore_canceled'):
            self.restore(cancel=canceled)
        self.assertFalse((self.root / 'staged-vault').exists())

    def test_tamper_and_existing_destination_fail_closed(self) -> None:
        self.prepare_stage()
        path = self.root / 'vault-backup' / 'inbox.enc'
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(MemoryFailure):
            self.restore()
        self.assertFalse((self.root / 'staged-vault').exists())
        path.write_bytes(original)
        target = self.root / 'staged-vault'
        target.mkdir()
        (target / 'sentinel').write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(MemoryFailure, 'backup_destination_exists'):
            self.restore()
        self.assertEqual((target / 'sentinel').read_text(encoding='utf-8'), 'keep')

    def test_running_service_rejects_restore(self) -> None:
        create_vault_backup(self.runtime, self.inbox, self.owner, self.root / 'vault-backup', self.root / 'backup-keys')
        self.start('--activate-owner', str(self.destination / 'mcp-access-owner.json'), '--activate-restored-without-vault')
        with self.assertRaisesRegex(MemoryFailure, 'service_already_running'):
            self.restore()
        self.assertFalse((self.root / 'staged-vault').exists())

    def test_non_admin_and_noninteractive_cli_are_rejected(self) -> None:
        self.prepare_stage()
        with self.assertRaises(MemoryFailure):
            restore_stage(self.destination / 'runtime.json', Access('synthetic-invalid', 'synthetic-invalid'),
                self.root / 'vault-backup', self.root / 'staged-vault', Path(self.keys.recovery_file),
                self.password, b'synthetic-vault-password-212')
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'backup-vault-stage', '--config',
            str(self.destination / 'runtime.json'), '--access', str(self.destination / 'mcp-access-owner.json'),
            '--source', str(self.root / 'vault-backup'), '--destination', str(self.root / 'staged-vault'),
            '--recovery-file', self.keys.recovery_file], input=b'', capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'interactive_recovery_required', result.stdout)
        self.assertFalse((self.root / 'staged-vault').exists())

    def test_later_protected_object_rejects_mismatched_ordinary_backup(self) -> None:
        self.inbox.submit(self.owner, Event(Source('owner', 'private', 'main', 'later'), 'user', 3, None,
                          'complete', 'synthetic-later-secret', sensitive=True))
        self.prepare_stage()
        with self.assertRaises(MemoryFailure):
            self.restore()
        self.assertFalse((self.root / 'staged-vault').exists())

    def test_cancel_during_unpack_and_failed_publication_preserve_source(self) -> None:
        self.prepare_stage()
        canceled = threading.Event()
        def cancel_after(*args: object, **kwargs: object) -> None:
            _decrypt(*args, **kwargs)  # type: ignore[arg-type]
            canceled.set()
        with patch('evernook.vault_restore._decrypt', side_effect=cancel_after):
            with self.assertRaisesRegex(MemoryFailure, 'vault_restore_canceled'):
                self.restore(cancel=canceled)
        with patch('evernook.vault_restore.os.rename', side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(MemoryFailure):
                self.restore()
        self.assertFalse((self.root / 'staged-vault').exists())
        self.assertTrue((self.root / 'vault-backup' / 'backup.json').exists())
        self.assertFalse(list(self.root.glob('.evernook-vault-restore-*')))

    def test_failure_after_rename_reports_unknown_outcome(self) -> None:
        self.prepare_stage()
        from os import rename
        def publish_then_fail(source: object, destination: object) -> None:
            rename(source, destination)  # type: ignore[arg-type]
            raise OSError('synthetic publication response failure')
        with patch('evernook.vault_restore.os.rename', side_effect=publish_then_fail):
            with self.assertRaisesRegex(MemoryFailure, 'vault_restore_outcome_unknown'):
                self.restore()
        self.assertTrue((self.root / 'staged-vault' / 'inbox' / 'inbox.sqlite3').exists())
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_decryption_checks_cancellation_during_digest_and_stream(self) -> None:
        _, recipient, _ = _writer(self.root / 'backup-keys')
        _, private, _ = _unlock(Path(self.keys.recovery_file), self.password)
        encrypted = self.root / 'large.enc'
        info = _encrypt(io.BytesIO(b'x' * (2 * 1024 * 1024)), encrypted, recipient, b'synthetic')
        calls = 0
        def check() -> None:
            nonlocal calls
            calls += 1
            if calls == 6:
                raise MemoryFailure('vault_restore_canceled')
        output = io.BytesIO()
        with self.assertRaisesRegex(MemoryFailure, 'vault_restore_canceled'):
            _decrypt(encrypted, output, private, info, b'synthetic', check=check)
        self.assertLess(len(output.getvalue()), 2 * 1024 * 1024)

    def test_process_crash_around_publication_preserves_missing_gate(self) -> None:
        self.prepare_stage()
        worker = Path(__file__).with_name('vault_restore_worker.py')
        for mode in ('before', 'after'):
            result = subprocess.run([sys.executable, str(worker), str(self.root), mode], capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 91, result.stderr.decode(errors='replace'))
            self.assertNotIn(self.private.text.encode(), result.stdout + result.stderr)
            target = self.root / ('crash-' + mode)
            self.assertEqual(target.exists(), mode == 'after')
            if mode == 'after':
                self.assertTrue((target / 'inbox' / 'inbox.sqlite3').exists())
        self.assertTrue(list(self.root.glob('.evernook-vault-restore-*')))
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')
