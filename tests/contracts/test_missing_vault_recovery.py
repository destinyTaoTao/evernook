from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import ProtectedInbox, initialize_protected_inbox
from evernook.service import ServiceClient


class MissingVaultRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-missing-vault-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'original', client_id='synthetic-owner', entry='owner', allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.password = b'synthetic-backup-password-212'
        self.keys = initialize_backup_keys(self.root / 'backup-keys', self.password)
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'vault', self.root / 'vault-keys', b'synthetic-vault-password-212')
        self.private = Event(Source('owner', 'private', 'main', 'one'), 'user', 1, None, 'complete',
                             'synthetic-private-sentinel-212', sensitive=True)
        self.protected_receipt = self.inbox.submit(self.owner, self.private)
        self.ordinary = Event(Source('owner', 'ordinary', 'main', 'two'), 'user', 2, None, 'complete', '合成普通记忆')
        self.ordinary_receipt = self.runtime.submit(self.owner, self.ordinary)
        create_backup(self.runtime, self.owner, self.root / 'backup', self.root / 'backup-keys')
        self.destination = self.root / 'restored'
        with OfflineRecovery(self.root / 'backup', self.destination, Path(self.keys.recovery_file), self.password) as review:
            self.assertTrue(review.review()['protected_references'])
            review.publish(ACKNOWLEDGEMENT)
        self.restored = MemoryRuntime(self.destination / 'runtime.json')
        self.new_owner = self.restored.access_from_file(self.destination / 'mcp-access-owner.json')
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)

    def command(self, *flags: str) -> list[str]:
        return [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.destination / 'runtime.json'), *flags]

    def start(self, *flags: str) -> ServiceClient:
        endpoint = self.destination / 'memory-service.json'
        previous = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen(self.command(*flags), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        until = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == previous:
            if process.poll() is not None or time.monotonic() >= until:
                detail = process.stderr.read().decode(errors='replace') if process.poll() is not None and process.stderr else 'timeout'
                self.fail('service failed to start: ' + detail)
            time.sleep(.05)
        return ServiceClient(self.destination / 'runtime.json')

    def test_explicit_missing_vault_mode_keeps_ordinary_service_and_blocks_private_access(self) -> None:
        owner_file = str(self.destination / 'mcp-access-owner.json')
        normal = subprocess.run(self.command('--activate-owner', owner_file), capture_output=True, timeout=30)
        self.assertNotEqual(normal.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', normal.stderr)
        client = self.start('--activate-owner', owner_file, '--activate-restored-without-vault')
        status = client.call(self.new_owner, 'status', {})['service']
        self.assertEqual(status['protected_state'], 'restored_missing')
        self.assertFalse(status['protected_capture'])
        management = client.call(self.new_owner, 'cli', {'command': 'manage', 'input': {'action': 'read', 'view': 'status'}})
        self.assertEqual(management['protected_state'], 'restored_missing')
        self.assertEqual(client.call(self.new_owner, 'read', {'message_id': self.ordinary_receipt.message_id})['event']['text'], self.ordinary.text)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.call(self.new_owner, 'protected-receive', {'event': asdict(self.private)})
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        bound = subprocess.run(self.command('--protected-inbox', str(self.root / 'vault'), '--protected-keys', str(self.root / 'vault-keys')),
                               capture_output=True, timeout=30)
        self.assertNotEqual(bound.returncode, 0)
        self.assertIn(b'protected_restore_required', bound.stderr)
        restarted = self.start()
        self.assertEqual(restarted.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        self.assertNotIn(self.private.text.encode(), b''.join(p.read_bytes() for p in self.destination.iterdir() if p.is_file()))

    def test_unrelated_ledger_cannot_enter_missing_vault_mode(self) -> None:
        from evernook import pair_client
        owner_file = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id=self.owner.client_id, entry='owner', access_path=owner_file)
        result = subprocess.run([sys.executable, '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(owner_file), '--activate-restored-without-vault'], capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_non_admin_cannot_activate_missing_vault_mode(self) -> None:
        from evernook import pair_client
        limited = self.destination / 'mcp-access-limited.json'
        pair_client(self.destination / 'runtime.json', client_id='synthetic-limited', entry='limited',
                    access_path=limited, scopes=('capture', 'recall'))
        result = subprocess.run(self.command('--activate-owner', str(limited), '--activate-restored-without-vault'),
                                capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'permission_denied', result.stderr)
        self.assertNotIn('service_mode', json.loads((self.destination / 'runtime.json').read_text()))

    def test_registry_only_restored_ledger_rejects_legacy_dual_writer(self) -> None:
        config, access = initialize(self.root / 'registry-only', client_id='synthetic-registry', entry='registry', allow_archive_review=True)
        runtime = MemoryRuntime(config)
        with runtime._connection() as db:
            db.execute('CREATE TABLE protected_inbox_registry_v1 (inbox_id TEXT NOT NULL PRIMARY KEY, key_id TEXT NOT NULL)')
            db.execute("INSERT INTO protected_inbox_registry_v1 VALUES ('synthetic-inbox', 'synthetic-key')")
        backup = self.root / 'registry-backup'
        create_backup(runtime, access, backup, self.root / 'backup-keys')
        restored = self.root / 'registry-restored'
        with OfflineRecovery(backup, restored, Path(self.keys.recovery_file), self.password) as review:
            review.publish(ACKNOWLEDGEMENT)
        process = subprocess.Popen([sys.executable, '-m', 'evernook.service', '--config', str(restored / 'runtime.json'),
                                    '--allow-legacy-shared'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, error = process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b'protected_cutover_requires_migration', error)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=15)

    def test_receipt_tampering_rejects_activation_and_restart(self) -> None:
        path = self.destination / 'recovery-receipt.json'
        original = path.read_bytes()
        value = json.loads(original)
        value['vault_restored'] = True
        path.write_text(json.dumps(value), encoding='utf-8')
        owner = str(self.destination / 'mcp-access-owner.json')
        result = subprocess.run(self.command('--activate-owner', owner, '--activate-restored-without-vault'), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)
        self.assertNotIn('service_mode', json.loads((self.destination / 'runtime.json').read_text()))
        path.write_bytes(original)
        client = self.start('--activate-owner', owner, '--activate-restored-without-vault')
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        path.unlink()
        result = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)

    def test_deleted_marker_row_is_not_treated_as_unconfigured(self) -> None:
        owner = str(self.destination / 'mcp-access-owner.json')
        self.start('--activate-owner', owner, '--activate-restored-without-vault')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        with sqlite3.connect(self.destination / 'memory.sqlite3') as db:
            db.execute('DELETE FROM restored_missing_vault_v1')
        db.close()
        result = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)
        with sqlite3.connect(self.destination / 'memory.sqlite3') as db:
            db.execute('DROP TABLE restored_missing_vault_v1')
        db.close()
        result = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)
        (self.destination / 'recovery-receipt.json').unlink()
        result = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)

    def test_crash_after_marker_before_mode_can_only_resume_explicitly(self) -> None:
        from unittest.mock import patch
        from evernook.vault_local import LocalDisplayRequest
        entry_file = self.destination / 'mcp-access-vault-entry.json'
        entry_access = pair_client(self.destination / 'runtime.json', client_id='synthetic-vault-entry', entry='owner', access_path=entry_file)
        preexisting = ProtectedInbox(self.restored, self.root / 'vault', self.root / 'vault-keys')
        display = LocalDisplayRequest(preexisting, entry_access, self.protected_receipt.object_id, 'synthetic', desktop=lambda: True)
        worker = Path(__file__).with_name('missing_vault_worker.py')
        config = self.destination / 'runtime.json'
        owner = self.destination / 'mcp-access-owner.json'
        result = subprocess.run([sys.executable, str(worker), str(config), str(owner), 'before'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 87, result.stderr)
        self.assertNotIn('service_mode', json.loads(config.read_text(encoding='utf-8')))
        with patch('evernook.vault_local._unlock') as unlock:
            with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
                display.display_once(b'synthetic-vault-password-212', lambda text: None)
            unlock.assert_not_called()
        from evernook.vault_verify import verify_page
        with patch('evernook.vault_verify._unlock') as unlock:
            with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
                verify_page(preexisting, self.new_owner, b'synthetic-vault-password-212')
            unlock.assert_not_called()
        for action in (lambda: preexisting.register(self.new_owner),
                       lambda: preexisting.stage(entry_access, self.private),
                       lambda: preexisting.reconcile(entry_access, self.protected_receipt.object_id),
                       lambda: preexisting.status(entry_access, self.protected_receipt.object_id)):
            with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
                action()
        with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
            initialize_protected_inbox(self.restored, self.root / 'new-vault', self.root / 'new-vault-keys', b'synthetic-new-password')
        self.assertFalse((self.root / 'new-vault').exists())
        self.assertFalse((self.root / 'new-vault-keys').exists())
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime
        checkpoint = capture_control_checkpoint(self.restored, self.new_owner)
        source = self.root / 'marker-archive'
        reviewed = self.root / 'marker-reviewed'
        self.restored.export_archive(self.new_owner, source)
        with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
            publish_replayed_runtime(self.restored, self.new_owner, source, self.root / 'marker-direct',
                                     checkpoint=checkpoint, client_id='synthetic', entry='synthetic')
        self.assertFalse((self.root / 'marker-direct').exists())
        replay_archive_controls(self.restored, self.new_owner, source, reviewed, checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
            publish_replayed_runtime(self.restored, self.new_owner, reviewed, self.root / 'wrong-republished',
                                     checkpoint=checkpoint, client_id='synthetic', entry='synthetic')
        self.assertFalse((self.root / 'wrong-republished').exists())
        normal = subprocess.run(self.command('--activate-owner', str(owner)), capture_output=True, timeout=30)
        self.assertNotEqual(normal.returncode, 0)
        self.assertIn(b'protected_restore_required', normal.stderr)
        client = self.start('--activate-owner', str(owner), '--activate-restored-without-vault')
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_crash_after_mode_write_restarts_with_missing_state(self) -> None:
        worker = Path(__file__).with_name('missing_vault_worker.py')
        config = self.destination / 'runtime.json'
        owner = self.destination / 'mcp-access-owner.json'
        result = subprocess.run([sys.executable, str(worker), str(config), str(owner), 'after'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 88, result.stderr)
        self.assertTrue(json.loads(config.read_text(encoding='utf-8'))['service_mode'])
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_new_restore_from_snapshot_rebinds_device_specific_marker(self) -> None:
        owner = self.destination / 'mcp-access-owner.json'
        self.start('--activate-owner', str(owner), '--activate-restored-without-vault')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        # Service-owned access is used only for this stopped synthetic fixture;
        # a public backup API for an activated service is a separate concern.
        stopped = MemoryRuntime(self.destination / 'runtime.json', service_owner=True)
        create_backup(stopped, self.new_owner, self.root / 'second-backup', self.root / 'backup-keys')
        next_device = self.root / 'second-restored'
        with OfflineRecovery(self.root / 'second-backup', next_device, Path(self.keys.recovery_file), self.password) as review:
            self.assertTrue(review.review()['protected_references'])
            review.publish(ACKNOWLEDGEMENT)
        self.destination = next_device
        self.restored = MemoryRuntime(next_device / 'runtime.json')
        self.new_owner = self.restored.access_from_file(next_device / 'mcp-access-owner.json')
        client = self.start('--activate-owner', str(next_device / 'mcp-access-owner.json'), '--activate-restored-without-vault')
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')
