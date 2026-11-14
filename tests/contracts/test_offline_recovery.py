from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.service import ServiceClient


class OfflineRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-offline-recovery-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.old_access = initialize(self.root / 'original', client_id='synthetic-old', entry='test', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.password = b'synthetic-recovery-password-456'
        self.keys = initialize_backup_keys(self.root / 'keys', self.password)
        self.event = Event(Source('test', 'session', 'main', 'one'), 'user', 1, None, 'complete', '合成恢复材料一')

    def backup(self) -> Path:
        root = self.root / 'backup'
        create_backup(self.runtime, self.old_access, root, self.root / 'keys')
        return root

    def test_publish_offline_preserves_snapshot_and_replaces_old_identity(self) -> None:
        from evernook.automatic_preferences import PreferenceUpdates
        from evernook.scheduler import ConsolidationScheduler
        preferences = PreferenceUpdates(self.runtime)
        preferences.configure(self.old_access, enabled=True, admitted_processor=preferences.processor.processor_version)
        visible = self.runtime.submit(self.old_access, self.event)
        hidden = self.runtime.submit(self.old_access, replace(self.event, source=replace(self.event.source, event='hidden'), text='合成隐藏材料'))
        instruction = self.runtime.submit(self.old_access, replace(self.event, source=replace(self.event.source, event='instruction'), text='合成撤回指令'))
        self.runtime.change(self.old_access, Change('synthetic-suppress', hidden.message_id, 1, 'suppress', instruction.message_id, 'synthetic'))
        backup = self.backup()
        self.config.parent.rename(self.root / 'original-offline')
        destination = self.root / 'new-device'
        with OfflineRecovery(backup, destination, Path(self.keys.recovery_file), self.password) as recovery:
            review = recovery.review()
            self.assertEqual(review['messages'], 3)
            self.assertEqual(review['later_changes'], 'unknown')
            self.assertFalse(review['vault_restored'])
            self.assertNotIn('合成恢复材料一', json.dumps(review, ensure_ascii=False))
            with self.assertRaisesRegex(MemoryFailure, 'backup_confirmation_required'):
                recovery.publish('not-confirmed')
            self.assertFalse(destination.exists())
            result = recovery.publish(ACKNOWLEDGEMENT)
            self.assertNotIn(self.password.decode(), json.dumps(result))
            self.assertNotIn(self.old_access.token, json.dumps(result))
            self.assertEqual(result, recovery.publish(ACKNOWLEDGEMENT))
        restored = MemoryRuntime(destination / 'runtime.json')
        owner = restored.access_from_file(destination / 'mcp-access-owner.json')
        self.assertTrue(ConsolidationScheduler(restored).status(owner).paused)
        self.assertIsNone(PreferenceUpdates(restored).settings(owner).admitted_processor)
        self.assertFalse(PreferenceUpdates(restored).settings(owner).enabled)
        self.assertNotEqual(owner.token, self.old_access.token)
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized'):
            restored.read(self.old_access, visible.message_id)
        self.assertEqual(restored.read(owner, visible.message_id).event.text, self.event.text)  # type: ignore[union-attr]
        self.assertIsNone(restored.read(owner, hidden.message_id))
        from evernook.runtime import pair_client
        pair_client(destination / 'runtime.json', client_id='synthetic-new-client', entry='synthetic-new-entry',
                    access_path=destination / 'mcp-access-new.json', scopes=('capture', 'recall'))
        ConsolidationScheduler(restored).pause(owner, paused=False)
        service = subprocess.Popen([sys.executable, '-m', 'evernook.service', '--config', str(destination / 'runtime.json'),
            '--activate-owner', str(destination / 'mcp-access-owner.json')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + 20
            while not (destination / 'memory-service.json').exists():
                if service.poll() is not None or time.monotonic() > end:
                    self.fail('restored service did not start')
                time.sleep(.05)
            result = ServiceClient(destination / 'runtime.json').call(owner, 'read', {'message_id': visible.message_id})
            self.assertEqual(result['event']['text'], self.event.text)
        finally:
            service.terminate()
            service.wait(timeout=15)
            if service.stderr:
                service.stderr.close()
        with OfflineRecovery(backup, destination, Path(self.keys.recovery_file), self.password) as retry:
            self.assertEqual(result['event']['text'], self.event.text)
            self.assertEqual(retry.publish(ACKNOWLEDGEMENT)['access_file'], str((destination / 'mcp-access-owner.json').resolve()))
            self.assertEqual(retry.publish(ACKNOWLEDGEMENT)['maintenance_at_recovery'], 'paused')

    def test_changed_backup_and_unrelated_destination_are_rejected(self) -> None:
        self.runtime.submit(self.old_access, self.event)
        backup = self.backup()
        destination = self.root / 'new-device'
        with OfflineRecovery(backup, destination, Path(self.keys.recovery_file), self.password) as recovery:
            from evernook.paths import provision_private_directory
            provision_private_directory(destination)
            (destination / 'keep.txt').write_text('synthetic-existing', encoding='utf-8')
            with self.assertRaises(MemoryFailure):
                recovery.publish(ACKNOWLEDGEMENT)
            self.assertEqual((destination / 'keep.txt').read_text(encoding='utf-8'), 'synthetic-existing')
            (destination / 'keep.txt').unlink()
            destination.rmdir()
            manifest = recovery._archive / 'manifest.json'
            original = manifest.read_bytes()
            manifest.write_bytes(original + b' ')
            with self.assertRaisesRegex(MemoryFailure, 'backup_changed_during_review'):
                recovery.publish(ACKNOWLEDGEMENT)
            manifest.write_bytes(original)
            with (backup / 'runtime.enc').open('ab') as target:
                target.write(b'synthetic-tamper')
            with self.assertRaisesRegex(MemoryFailure, 'backup_changed_during_review'):
                recovery.publish(ACKNOWLEDGEMENT)
            self.assertFalse(destination.exists())

    def test_private_registry_is_disclosed_without_claiming_vault_restore(self) -> None:
        self.runtime.submit(self.old_access, self.event)
        with self.runtime._connection() as db:
            db.execute('CREATE TABLE protected_inbox_registry_v1 (inbox_id TEXT NOT NULL PRIMARY KEY, key_id TEXT NOT NULL)')
            db.execute("INSERT INTO protected_inbox_registry_v1 VALUES ('synthetic-inbox', 'synthetic-key')")
        backup = self.backup()
        with OfflineRecovery(backup, self.root / 'new-device', Path(self.keys.recovery_file), self.password) as recovery:
            self.assertTrue(recovery.review()['protected_references'])
            self.assertFalse(recovery.review()['vault_restored'])
            self.assertTrue(recovery.publish(ACKNOWLEDGEMENT)['protected_references'])
        destination = self.root / 'new-device'
        service = subprocess.run([sys.executable, '-m', 'evernook.service', '--config', str(destination / 'runtime.json'),
            '--activate-owner', str(destination / 'mcp-access-owner.json')], capture_output=True, timeout=20)
        self.assertNotEqual(service.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', service.stdout + service.stderr)

    def test_crash_before_and_after_atomic_publish_can_be_retried(self) -> None:
        receipt = self.runtime.submit(self.old_access, self.event)
        backup = self.backup()
        worker = Path(__file__).with_name('offline_recovery_worker.py')
        for phase, code in [('before', 85), ('after', 86)]:
            destination = self.root / ('device-' + phase)
            child = subprocess.run([sys.executable, str(worker), str(backup), str(destination),
                self.keys.recovery_file, phase], capture_output=True, timeout=45)
            self.assertEqual(child.returncode, code, child.stderr)
            self.assertEqual(destination.exists(), phase == 'after')
            if destination.exists():
                owner_file = (destination / 'mcp-access-owner.json').read_bytes()
            with OfflineRecovery(backup, destination, Path(self.keys.recovery_file), self.password) as recovery:
                recovery.publish(ACKNOWLEDGEMENT)
            if phase == 'after':
                self.assertEqual((destination / 'mcp-access-owner.json').read_bytes(), owner_file)
            restored = MemoryRuntime(destination / 'runtime.json')
            owner = restored.access_from_file(destination / 'mcp-access-owner.json')
            self.assertIsNotNone(restored.read(owner, receipt.message_id))

    def test_cli_without_terminal_rejects_and_does_not_create_destination(self) -> None:
        self.runtime.submit(self.old_access, self.event)
        backup = self.backup()
        destination = self.root / 'new-device'
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'backup-restore-runtime', '--source', str(backup),
            '--destination', str(destination), '--recovery-file', self.keys.recovery_file], input=b'', capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'interactive_recovery_required', result.stdout + result.stderr)
        self.assertFalse(destination.exists())

    def test_wrong_recovery_password_never_creates_runtime(self) -> None:
        self.runtime.submit(self.old_access, self.event)
        backup = self.backup()
        destination = self.root / 'new-device'
        with self.assertRaisesRegex(MemoryFailure, 'recovery_unlock_failed'):
            OfflineRecovery(backup, destination, Path(self.keys.recovery_file), b'synthetic-wrong-password')
        self.assertFalse(destination.exists())

    def test_unlinked_protected_projection_is_disclosed_and_never_published(self) -> None:
        receipt = self.runtime.submit(self.old_access, self.event)
        with self.runtime._connection() as db:
            db.execute("UPDATE messages SET event=json_set(event,'$.text',?) WHERE id=?",
                       ('[PROTECTED:ENCRYPTED_ORIGINAL]', receipt.message_id))
        backup = self.backup()
        destination = self.root / 'new-device'
        with OfflineRecovery(backup, destination, Path(self.keys.recovery_file), self.password) as recovery:
            self.assertTrue(recovery.review()['protected_orphan'])
            self.assertTrue(recovery.review()['protected_references'])
            with self.assertRaisesRegex(MemoryFailure, 'backup_protected_orphan_requires_review'):
                recovery.publish(ACKNOWLEDGEMENT)
        self.assertFalse(destination.exists())

    def test_cli_interactive_review_requires_exact_confirmation_without_secret_output(self) -> None:
        import io
        from unittest.mock import patch
        from evernook.cli import _execute, _parser
        self.runtime.submit(self.old_access, self.event)
        backup = self.backup()
        destination = self.root / 'new-device'
        args = _parser().parse_args(['backup-restore-runtime', '--source', str(backup), '--destination', str(destination),
                                     '--recovery-file', self.keys.recovery_file])
        stderr = io.StringIO()
        with patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=self.password.decode()), \
                patch('builtins.input', return_value=ACKNOWLEDGEMENT), patch('sys.stderr', stderr):
            result = _execute(args)
        assert isinstance(result, dict)
        self.assertFalse(result['vault_restored'])
        self.assertIn('unknown', stderr.getvalue())
        self.assertNotIn(self.password.decode(), stderr.getvalue())
        self.assertNotIn(self.event.text, stderr.getvalue())
