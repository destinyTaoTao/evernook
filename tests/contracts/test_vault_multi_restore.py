from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import zipfile

from evernook import Access, Change, Event, MemoryFailure, MemoryRuntime, Source, pair_client
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.service import ServiceClient
from evernook.vault_multi_restore import restore_stage
from evernook.vault_workspace import cleanup_workspaces, list_workspaces
from tests.contracts.test_vault_multi_service import MultiVaultServiceTests


class MultiVaultRestoreTests(MultiVaultServiceTests):
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


if __name__ == '__main__':
    unittest.main()
