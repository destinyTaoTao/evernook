from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from evernook import Access, Change, Event, MemoryFailure, MemoryRuntime, Source, pair_client
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.service import ServiceClient
from evernook.runtime import _json
from evernook.vault_multi_restore import restore_stage
from evernook.vault_stage_check import _locked_read, _verified_locked, check_stage
from evernook.vault_restore import _stopped_service
from evernook.vault_workspace import cleanup_workspaces, list_workspaces
from tests.contracts.test_vault_multi_service import MultiVaultServiceFixture


class MultiVaultRestoreTests(MultiVaultServiceFixture):
    def test_verified_stage_preserves_missing_gate_and_rejects_wrong_password(self) -> None:
        client = self.start(activate=True)
        keys = initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        ordinary_backup, vault_backup = self.root / 'ordinary-backup', self.root / 'vault-backup'
        create_backup(MemoryRuntime(self.config, service_owner=True), self.owner, ordinary_backup, self.root / 'backup-keys')
        client.protected_backup(self.owner, vault_backup, self.root / 'backup-keys')
        client.protected_receive(self.owner, replace(self.event, source=replace(self.event.source, event='after-backup')))
        mismatch_backup = self.root / 'mismatch-vault-backup'
        client.protected_backup(self.owner, mismatch_backup, self.root / 'backup-keys')
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        recovered = self.root / 'recovered'
        with OfflineRecovery(ordinary_backup, recovered, Path(keys.recovery_file), b'synthetic backup password') as review:
            review.publish(ACKNOWLEDGEMENT)
        config, owner_file = recovered / 'runtime.json', recovered / 'mcp-access-owner.json'
        owner = MemoryRuntime(config).access_from_file(owner_file)
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                                    '--activate-owner', str(owner_file), '--activate-restored-without-vault'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while not (recovered / 'memory-service.json').exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail('synthetic restored service failed')
            time.sleep(.05)
        self.assertEqual(ServiceClient(config).call(owner, 'status', {})['service']['protected_state'], 'restored_missing')
        process.terminate(); process.wait(timeout=15)
        entry_access = pair_client(config, client_id='restored-owner-entry', entry='owner',
                                   access_path=recovered / 'mcp-access-old-entry.json', service_owner=True)
        restored_runtime = MemoryRuntime(config, service_owner=True)
        instruction = restored_runtime.submit(entry_access, Event(Source('owner', 'synthetic', 'main', 'suppress'),
                                              'user', 5, None, 'complete', 'synthetic suppression'))
        assert self.linked.message is not None
        restored_runtime.change(entry_access, Change('synthetic-suppress-stage', self.linked.message.message_id,
                                                       1, 'suppress', instruction.message_id, 'synthetic'))
        destination = self.root / 'staged-set'
        by_id = {self.first.inbox_id: self.passwords[0], self.second.inbox_id: self.passwords[1]}
        def password(_slot: str, inbox_id: str) -> bytes:
            return by_id[inbox_id]
        canceled = threading.Event()
        canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'vault_restore_canceled'):
            restore_stage(config, owner, vault_backup, destination, Path(keys.recovery_file),
                          b'synthetic backup password', password, cancel=canceled)
        with self.assertRaises(MemoryFailure):
            restore_stage(config, Access('unauthorized', 'unauthorized'), vault_backup, destination,
                          Path(keys.recovery_file), b'synthetic backup password', password)
        with self.assertRaises(MemoryFailure):
            restore_stage(config, owner, mismatch_backup, destination, Path(keys.recovery_file),
                          b'synthetic backup password', password)
        self.assertFalse(destination.exists())
        encrypted = vault_backup / '00-inbox.enc'
        saved = encrypted.read_bytes()
        encrypted.write_bytes(saved[:-1] + bytes([saved[-1] ^ 1]))
        try:
            with self.assertRaises(MemoryFailure):
                restore_stage(config, owner, vault_backup, destination, Path(keys.recovery_file),
                              b'synthetic backup password', password)
            self.assertFalse(destination.exists())
        finally:
            encrypted.write_bytes(saved)
        worker = Path(__file__).with_name('vault_multi_restore_crash_worker.py')
        crashed = subprocess.run([sys.executable, str(worker), str(self.root)], capture_output=True, timeout=90)
        self.assertEqual(crashed.returncode, 91, crashed.stderr.decode(errors='replace'))
        self.assertFalse((self.root / 'crash-stage').exists())
        entries = list_workspaces(config, owner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(cleanup_workspaces(config, owner, [entries[0]['id']])['removed'], 1)
        published = subprocess.run([sys.executable, str(worker), str(self.root), 'published',
                                    self.first.inbox_id, self.second.inbox_id], capture_output=True, timeout=120)
        self.assertEqual(published.returncode, 92, published.stderr.decode(errors='replace'))
        self.assertTrue((self.root / 'crash-published').is_file())
        entries = list_workspaces(config, owner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(cleanup_workspaces(config, owner, [entries[0]['id']])['removed'], 1)
        with self.assertRaises(MemoryFailure):
            restore_stage(config, owner, vault_backup, destination, Path(keys.recovery_file),
                          b'synthetic backup password', lambda *_: b'synthetic wrong vault password')
        self.assertFalse(destination.exists())
        result = restore_stage(config, owner, vault_backup, destination, Path(keys.recovery_file),
                               b'synthetic backup password', password)
        self.assertEqual(result.inboxes, 2)
        self.assertEqual(result.protected_objects, 3)
        self.assertFalse(result.attached)
        self.assertFalse(result.daily_admitted)
        self.assertTrue(destination.is_file())
        with zipfile.ZipFile(destination) as archive:
            self.assertIn('stage-proof.json', archive.namelist())
            self.assertIn('00-inbox.enc', archive.namelist())
        self.assertNotIn(self.event.text.encode(), destination.read_bytes())
        runtime = MemoryRuntime(config, service_owner=True)
        self.assertEqual(runtime.state(entry_access, self.linked.message.message_id).status, 'suppressed')
        with runtime._connection() as core:
            from evernook.missing_vault import read_state
            self.assertTrue(read_state(runtime, core))
        checked = check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password')
        self.assertEqual(checked['inboxes'], 2)
        self.assertFalse(checked['attachment_authorized'])
        self.assertFalse(checked['vault_passwords_rechecked'])
        with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
            with closing(sqlite3.connect(':memory:', isolation_level=None)) as wrong_core:
                wrong_core.execute('BEGIN IMMEDIATE')
                with _locked_read(destination) as stream:
                    with self.assertRaisesRegex(MemoryFailure, 'vault_stage_lock_required'):
                        with _verified_locked(runtime, owner, wrong_core, stream, Path(keys.recovery_file), b'synthetic backup password',
                                              check=lambda: None, deadline=time.monotonic() + 120):
                            pass
            with _locked_read(destination) as stream:
                with self.assertRaisesRegex(MemoryFailure, 'vault_stage_lock_required'):
                    with _verified_locked(runtime, owner, core, stream, Path(keys.recovery_file), b'synthetic backup password',
                                          check=lambda: None, deadline=time.monotonic() + 120):
                        pass
            core.execute('BEGIN IMMEDIATE')
            with _locked_read(destination) as stream:
                with _verified_locked(runtime, owner, core, stream, Path(keys.recovery_file), b'synthetic backup password',
                                      check=lambda: None, deadline=time.monotonic() + 120) as verified:
                    self.assertFalse(verified.result['attachment_authorized'])
                    self.assertIn('00-inbox.enc', verified.archive.namelist())
                    child = subprocess.run([sys.executable, '-c',
                        'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"changed")', str(destination)],
                        capture_output=True, timeout=15)
                    self.assertNotEqual(child.returncode, 0)
            core.execute('ROLLBACK')
            core.execute('BEGIN IMMEDIATE')
            with patch('evernook.vault_stage_check._authorize_admin', side_effect=[None, MemoryFailure('permission_denied')]):
                with _locked_read(destination) as stream:
                    with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                        with _verified_locked(runtime, owner, core, stream, Path(keys.recovery_file), b'synthetic backup password',
                                              check=lambda: None, deadline=time.monotonic() + 120):
                            pass
            core.execute('ROLLBACK')
            core.execute('BEGIN IMMEDIATE')
            core.execute('UPDATE messages SET fingerprint=? WHERE id=?', (bytes(32), self.linked.message.message_id))
            interrupted = threading.Event()
            with _locked_read(destination) as stream:
                with self.assertRaisesRegex(MemoryFailure, 'vault_backup_ledger_mismatch'):
                    with _verified_locked(runtime, owner, core, stream, Path(keys.recovery_file), b'synthetic backup password',
                                          check=lambda: None, deadline=time.monotonic() + 120, cancel=interrupted):
                        pass
            interrupted.set()
            value = core.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<3000) SELECT sum(x) FROM n').fetchone()[0]
            self.assertEqual(value, 4501500)
            core.execute('ROLLBACK')
        with patch('evernook.vault_stage_check._receipt', return_value='0' * 64):
            with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password')
        old_stage = self.root / 'old-stage'
        with zipfile.ZipFile(destination) as original, zipfile.ZipFile(old_stage, 'w', compression=zipfile.ZIP_STORED) as old:
            for name in original.namelist():
                data = original.read(name)
                if name == 'stage-proof.json':
                    current = json.loads(data)['content']
                    old_content = {'version': 1, 'backup_id': current['backup_id'], 'binding': current['binding']}
                    authentication = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-stage-v1\0'
                                                 + _json(old_content).encode(), hashlib.sha256).hex()
                    data = _json({'content': old_content, 'authentication': authentication}).encode()
                old.writestr(name, data)
        with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
            check_stage(config, owner, old_stage, Path(keys.recovery_file), b'synthetic backup password')
        with self.assertRaises(MemoryFailure):
            check_stage(config, Access('unauthorized', 'unauthorized'), destination, Path(keys.recovery_file), b'synthetic backup password')
        with self.assertRaises(MemoryFailure):
            check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic wrong backup password')
        other_keys = initialize_backup_keys(self.root / 'other-backup-keys', b'synthetic other backup password')
        with self.assertRaises(MemoryFailure):
            check_stage(config, owner, destination, Path(other_keys.recovery_file), b'synthetic other backup password')
        canceled = threading.Event(); canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'vault_restore_canceled'):
            check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password', cancel=canceled)
        with runtime._connection() as core:
            original_fingerprint = bytes(core.execute('SELECT fingerprint FROM messages WHERE id=?',
                                                       (self.linked.message.message_id,)).fetchone()['fingerprint'])
            core.execute('UPDATE messages SET fingerprint=? WHERE id=?', (bytes(32), self.linked.message.message_id))
        try:
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_ledger_mismatch'):
                check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password')
        finally:
            with runtime._connection() as core:
                core.execute('UPDATE messages SET fingerprint=? WHERE id=?', (original_fingerprint, self.linked.message.message_id))
        with runtime._connection() as core:
            original_event = core.execute('SELECT event FROM messages WHERE id=?', (self.linked.message.message_id,)).fetchone()['event']
            core.execute('UPDATE messages SET event=? WHERE id=?', ('{"text":"synthetic-tamper"}', self.linked.message.message_id))
        try:
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_ledger_mismatch'):
                check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password')
        finally:
            with runtime._connection() as core:
                core.execute('UPDATE messages SET event=? WHERE id=?', (original_event, self.linked.message.message_id))
        with runtime._connection() as core:
            core.execute('PRAGMA foreign_keys=OFF')
            core.execute('INSERT INTO protected_links_v1(message_id,reference) VALUES (?,?)', ('synthetic-orphan-link', '{}'))
        try:
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_ledger_mismatch'):
                check_stage(config, owner, destination, Path(keys.recovery_file), b'synthetic backup password')
        finally:
            with runtime._connection() as core:
                core.execute('DELETE FROM protected_links_v1 WHERE message_id=?', ('synthetic-orphan-link',))
        damaged = self.root / 'damaged-stage'
        raw = destination.read_bytes()
        damaged.write_bytes(raw[:50] + bytes([raw[50] ^ 1]) + raw[51:])
        with self.assertRaises(MemoryFailure):
            check_stage(config, owner, damaged, Path(keys.recovery_file), b'synthetic backup password')


if __name__ == '__main__':
    unittest.main()
