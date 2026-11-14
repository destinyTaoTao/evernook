from __future__ import annotations

import io
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evernook import Access, Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.backup import _decrypt, _encrypt, _unlock, _writer, create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import initialize_protected_inbox
from evernook.service import ServiceClient
from evernook.service import client_access
from evernook.protected import ProtectedInbox
from evernook.vault_local import LocalDisplayRequest
from evernook.runtime import _identities, _load
from evernook.vault_backup import create_vault_backup
from evernook.vault_restore import VaultRestoreStage, restore_stage
from evernook.vault_attach import attach
from evernook.vault_workspace import cleanup_workspaces, list_workspaces


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
        self.private_receipt = self.inbox.submit(self.owner, self.private)
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

    def test_stream_provenance_survives_backup_restore_and_attach(self) -> None:
        event = replace(self.private, source=replace(self.private.source, event='stream'))
        stream = self.inbox.submit(self.owner, event, stream_provenance=True)
        create_backup(self.runtime, self.owner, self.root / 'backup-with-stream', self.root / 'backup-keys')
        self.destination = self.root / 'restored-with-stream'
        with OfflineRecovery(self.root / 'backup-with-stream', self.destination, Path(self.keys.recovery_file), self.password) as review:
            review.publish(ACKNOWLEDGEMENT)
        self.new_owner = MemoryRuntime(self.destination / 'runtime.json').access_from_file(self.destination / 'mcp-access-owner.json')
        self.prepare_stage()
        self.assertEqual(self.restore().protected_objects, 2)
        container = self.root / 'staged-vault'
        entry_access = pair_client(self.destination / 'runtime.json', client_id='synthetic-stream-entry', entry='owner',
                                   access_path=self.destination / 'mcp-access-stream-entry.json', service_owner=True)
        self.assertEqual(attach(self.destination / 'runtime.json', self.new_owner, container,
                                b'synthetic-vault-password-212').protected_objects, 2)
        restored = ProtectedInbox(MemoryRuntime(self.destination / 'runtime.json', service_owner=True),
                                  container / 'inbox', container / 'keys')
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            LocalDisplayRequest(restored, self.new_owner, stream.object_id, 'synthetic', desktop=lambda: True)
        request = LocalDisplayRequest(restored, entry_access, stream.object_id, 'synthetic', desktop=lambda: True)
        self.assertEqual(request.version, 2)
        displayed: list[str] = []
        request.display_once(b'synthetic-vault-password-212', displayed.append)
        self.assertEqual(displayed, [event.text])

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
        entries = list_workspaces(self.destination / 'runtime.json', self.new_owner)
        self.assertEqual(len(entries), 2)
        cleanup_result = cleanup_workspaces(self.destination / 'runtime.json', self.new_owner, [entry['id'] for entry in entries])
        self.assertEqual(cleanup_result['removed'], 2)
        self.assertTrue((self.root / 'crash-after' / 'inbox' / 'inbox.sqlite3').exists())
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_attach_reverifies_and_service_binds_exact_paths(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        attached = attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        self.assertEqual(attached.protected_objects, 1)
        self.assertFalse(attached.daily_admitted)
        retry = attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        self.assertEqual(attached, retry)
        no_vault = subprocess.run([sys.executable, '-m', 'evernook.service', '--config',
            str(self.destination / 'runtime.json')], capture_output=True, timeout=30)
        self.assertNotEqual(no_vault.returncode, 0)
        self.assertIn(b'protected_attachment_mismatch', no_vault.stderr)
        client = self.start('--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys'))
        status = client.call(self.new_owner, 'status', {})['service']
        self.assertEqual(status['protected_state'], 'configured')
        self.assertTrue(status['protected_capture'])
        page = client.protected_inventory(self.new_owner)
        self.assertEqual(len(page['objects']), 1)

    def test_attach_rejects_bad_password_and_modified_material(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        with self.assertRaises(MemoryFailure):
            attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-wrong-password')
        writer = container / 'keys' / 'writer.key'
        saved = writer.read_bytes()
        writer.write_bytes(bytes([saved[0] ^ 1]) + saved[1:])
        with self.assertRaises(MemoryFailure):
            attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        writer.write_bytes(saved)
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_service_rejects_old_inbox_snapshot_after_new_private_event(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        inbox_db = container / 'inbox' / 'inbox.sqlite3'
        old = self.root / 'old-synthetic-inbox.sqlite3'
        with closing(sqlite3.connect(inbox_db)) as source, closing(sqlite3.connect(old)) as target:
            source.backup(target)
        flags = ('--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys'))
        client = self.start(*flags)
        entry = _identities(_load(self.destination / 'client.json'))[self.new_owner.client_id].entry
        client.protected_receive(self.new_owner, Event(Source(entry, 'private', 'main', 'later'), 'user', 3, None,
                                 'complete', 'synthetic-later-secret', sensitive=True))
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        with closing(sqlite3.connect(old)) as source, closing(sqlite3.connect(inbox_db)) as target:
            source.backup(target)
        result = subprocess.run([sys.executable, '-m', 'evernook.service', '--config',
            str(self.destination / 'runtime.json'), *flags], capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(b'protected_cutover_requires_migration' in result.stderr or b'protected_link_unavailable' in result.stderr)

    def test_attach_crash_on_either_side_of_commit_is_fail_closed(self) -> None:
        self.prepare_stage()
        self.restore()
        worker = Path(__file__).with_name('vault_attach_worker.py')
        before = subprocess.run([sys.executable, str(worker), str(self.root), 'before'], capture_output=True, timeout=90)
        self.assertEqual(before.returncode, 91, before.stderr.decode(errors='replace'))
        client = self.start()
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'restored_missing')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        after = subprocess.run([sys.executable, str(worker), str(self.root), 'after'], capture_output=True, timeout=90)
        self.assertEqual(after.returncode, 91, after.stderr.decode(errors='replace'))
        container = self.root / 'staged-vault'
        entries = list_workspaces(self.destination / 'runtime.json', self.new_owner)
        self.assertEqual(len(entries), 2)
        result = cleanup_workspaces(self.destination / 'runtime.json', self.new_owner, [entry['id'] for entry in entries])
        self.assertEqual(result['removed'], 2)
        self.assertTrue((container / 'keys' / 'recovery.json').exists())
        client = self.start('--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys'))
        self.assertEqual(client.call(self.new_owner, 'status', {})['service']['protected_state'], 'configured')

    def test_pending_private_write_survives_restart_before_and_after_link(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        worker = Path(__file__).with_name('vault_attach_pending_worker.py')
        crash = subprocess.run([sys.executable, str(worker), str(self.root)], capture_output=True, timeout=60)
        self.assertEqual(crash.returncode, 91, crash.stderr.decode(errors='replace'))
        flags = ('--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys'))
        entry = _identities(_load(self.destination / 'client.json'))[self.new_owner.client_id].entry
        event = Event(Source(entry, 'private', 'main', 'crash-pending'), 'user', 4, None,
                      'complete', 'synthetic-pending-secret', sensitive=True)
        client = self.start(*flags)
        receipt = client.protected_receive(self.new_owner, event)
        self.assertEqual(receipt['state'], 'protected_linked')
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        # Simulate interruption after ordinary link commit but before inbox acknowledgement.
        with closing(sqlite3.connect(container / 'inbox' / 'inbox.sqlite3')) as db:
            db.execute("UPDATE objects SET state='protected_pending_link',receipt=NULL WHERE id=?", (receipt['object_id'],))
            db.commit()
        client = self.start(*flags)
        self.assertEqual(client.protected_receive(self.new_owner, event)['state'], 'protected_linked')

    def test_subsequent_ordinary_backup_does_not_reuse_old_attachment_proof(self) -> None:
        self.prepare_stage()
        self.restore()
        attach(self.destination / 'runtime.json', self.new_owner, self.root / 'staged-vault', b'synthetic-vault-password-212')
        old_runtime = MemoryRuntime(self.destination / 'runtime.json', service_owner=True)
        backup = self.root / 'subsequent-backup'
        create_backup(old_runtime, self.new_owner, backup, self.root / 'backup-keys')
        second = self.root / 'second-restored'
        with OfflineRecovery(backup, second, Path(self.keys.recovery_file), self.password) as review:
            review.publish(ACKNOWLEDGEMENT)
        process = subprocess.Popen([sys.executable, '-m', 'evernook.service', '--config', str(second / 'runtime.json'),
            '--activate-owner', str(second / 'mcp-access-owner.json'), '--activate-restored-without-vault'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        endpoint, deadline = second / 'memory-service.json', time.monotonic() + 30
        while not endpoint.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                detail = process.stderr.read().decode(errors='replace') if process.poll() is not None and process.stderr else 'timeout'
                self.fail('second recovery failed: ' + detail)
            time.sleep(.05)
        access = MemoryRuntime(second / 'runtime.json', service_owner=True).access_from_file(second / 'mcp-access-owner.json')
        self.assertEqual(ServiceClient(second / 'runtime.json').call(access, 'status', {})['service']['protected_state'], 'restored_missing')

    def test_tampered_attachment_receipt_blocks_startup(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        with self.assertRaises(MemoryFailure):
            attach(self.destination / 'runtime.json', Access('invalid', 'invalid'), container, b'synthetic-vault-password-212')
        attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        with closing(sqlite3.connect(self.destination / 'memory.sqlite3')) as db:
            db.execute("UPDATE restored_vault_attachment_v1 SET authentication=?", ('0' * 64,))
            db.commit()
        result = subprocess.run([sys.executable, '-m', 'evernook.service', '--config', str(self.destination / 'runtime.json'),
            '--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys')],
            capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_recovery_proof_invalid', result.stderr)

    def test_repaired_source_identity_can_open_old_secret_locally(self) -> None:
        self.prepare_stage()
        self.restore()
        container = self.root / 'staged-vault'
        attach(self.destination / 'runtime.json', self.new_owner, container, b'synthetic-vault-password-212')
        client = self.start('--protected-inbox', str(container / 'inbox'), '--protected-keys', str(container / 'keys'))
        object_id = client.protected_inventory(self.new_owner)['objects'][0]['object_id']
        paired = client.call(self.new_owner, 'pair-client', {'client_id': 'synthetic-repaired-entry',
                             'entry': 'owner', 'scopes': ['capture', 'recall']})
        access = client_access(self.destination / 'runtime.json', Path(paired['access_file']))
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        runtime = MemoryRuntime(self.destination / 'runtime.json', service_owner=True)
        inbox = ProtectedInbox(runtime, container / 'inbox', container / 'keys')
        displayed: list[str] = []
        request = LocalDisplayRequest(inbox, access, object_id, 'synthetic-recovery-test', desktop=lambda: True)
        request.display_once(b'synthetic-vault-password-212', displayed.append)
        self.assertEqual(displayed, [self.private.text])

    def test_suppression_after_backup_remains_effective_after_attach(self) -> None:
        self.prepare_stage()
        self.restore()
        config = self.destination / 'runtime.json'
        access = pair_client(config, client_id='synthetic-old-entry', entry='owner',
                             access_path=self.destination / 'mcp-access-old-entry.json', service_owner=True)
        runtime = MemoryRuntime(config, service_owner=True)
        instruction = runtime.submit(access, Event(Source('owner', 'private', 'main', 'suppress'), 'user', 5, None,
                                     'complete', 'synthetic suppression instruction'))
        assert self.private_receipt.message is not None
        runtime.change(access, Change('synthetic-suppress-after-backup', self.private_receipt.message.message_id,
                                    1, 'suppress', instruction.message_id, 'synthetic'))
        container = self.root / 'staged-vault'
        attach(config, self.new_owner, container, b'synthetic-vault-password-212')
        inbox = ProtectedInbox(runtime, container / 'inbox', container / 'keys')
        with self.assertRaisesRegex(MemoryFailure, 'vault_source_unavailable'):
            LocalDisplayRequest(inbox, access, self.private_receipt.object_id, 'synthetic-recovery-test', desktop=lambda: True)
