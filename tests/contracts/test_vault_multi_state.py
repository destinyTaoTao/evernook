from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

from evernook import MemoryFailure, MemoryRuntime
from evernook.backup import create_backup, initialize_backup_keys
from evernook.missing_vault import multi_attachment, read_state
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import ProtectedInbox, initialize_protected_inbox
from evernook.runtime import _atomic_json, _load
from evernook.service import ServiceClient
from tests.contracts.test_vault_multi_service import MultiVaultServiceFixture


class MultiVaultStateTests(MultiVaultServiceFixture):
    def test_transaction_crash_restart_and_second_recovery(self) -> None:
        third = initialize_protected_inbox(self.runtime, self.root / 'unused-inbox', self.root / 'unused-keys',
                                           b'synthetic unused vault password')
        self.start(activate=True)
        keys = initialize_backup_keys(self.root / 'backup-keys', b'synthetic multi backup password')
        ordinary = self.root / 'ordinary-backup'
        create_backup(MemoryRuntime(self.config, service_owner=True), self.owner, ordinary, self.root / 'backup-keys')
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        recovered = self.root / 'recovered'
        with OfflineRecovery(ordinary, recovered, Path(keys.recovery_file), b'synthetic multi backup password') as review:
            review.publish(ACKNOWLEDGEMENT)
        config, owner_file = recovered / 'runtime.json', recovered / 'mcp-access-owner.json'
        runtime = MemoryRuntime(config, service_owner=True)
        owner = runtime.access_from_file(owner_file)
        def start_service(command: list[str], endpoint: Path) -> subprocess.Popen[bytes]:
            old = endpoint.read_bytes() if endpoint.exists() else None
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.processes.append(process)
            deadline = time.monotonic() + 30
            while not endpoint.exists() or endpoint.read_bytes() == old:
                if process.poll() is not None or time.monotonic() >= deadline:
                    assert process.stderr is not None
                    self.fail(process.stderr.read().decode(errors='replace') if process.poll() is not None else 'timeout')
                time.sleep(.05)
            return process
        process = start_service([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                                 '--activate-owner', str(owner_file), '--activate-restored-without-vault'],
                                recovered / 'memory-service.json')
        self.assertEqual(ServiceClient(config).call(owner, 'status', {})['service']['protected_state'], 'restored_missing')
        process.terminate(); process.wait(timeout=15)
        shutil.copyfile(self.config.parent / 'protected-multi-binding.json', recovered / 'protected-multi-binding.json')
        _atomic_json(config, {**_load(config), 'protected_multi_binding': True})
        with runtime._connection() as core:
            self.assertTrue(read_state(runtime, core))
        command = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                   '--protected-inbox', str(self.first.root), '--protected-keys', str(self.first.key_root),
                   '--protected-history-inbox', str(self.second.root), '--protected-history-keys', str(self.second.key_root)]
        blocked = subprocess.run(command, capture_output=True, timeout=30)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn(b'protected_restore_required', blocked.stderr + blocked.stdout)
        worker = Path(__file__).with_name('vault_multi_state_crash_worker.py')
        before = subprocess.run([sys.executable, str(worker), str(config), 'before'], capture_output=True, timeout=45)
        self.assertEqual(before.returncode, 91, before.stderr.decode(errors='replace'))
        with runtime._connection() as core:
            self.assertTrue(read_state(runtime, core))
            self.assertIsNone(multi_attachment(runtime, core))
        after = subprocess.run([sys.executable, str(worker), str(config), 'after'], capture_output=True, timeout=45)
        self.assertEqual(after.returncode, 92, after.stderr.decode(errors='replace'))
        with runtime._connection() as core:
            self.assertFalse(read_state(runtime, core))
            self.assertIsNotNone(multi_attachment(runtime, core))
            core.execute('BEGIN IMMEDIATE')
            core.execute('DROP TABLE restored_multi_attachment_v1')
            with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                read_state(runtime, core)
            core.execute('ROLLBACK')
        with self.assertRaisesRegex(MemoryFailure, 'protected_attachment_mismatch'):
            ProtectedInbox(runtime, third.root, third.key_root)
        receiving = self.first.key_root / 'receiving.json'
        original_receiving = receiving.read_bytes()
        receiving.write_bytes((third.key_root / 'receiving.json').read_bytes())
        try:
            with self.assertRaisesRegex(MemoryFailure, 'protected_attachment_mismatch'):
                ProtectedInbox(runtime, self.first.root, self.first.key_root)
        finally:
            receiving.write_bytes(original_receiving)
        with closing(sqlite3.connect(self.first.root / 'inbox.sqlite3')) as db:
            db.execute('UPDATE settings SET inbox_id=? WHERE singleton=1', (third.inbox_id,))
            db.commit()
        try:
            with self.assertRaisesRegex(MemoryFailure, 'protected_attachment_mismatch'):
                ProtectedInbox(runtime, self.first.root, self.first.key_root)
        finally:
            with closing(sqlite3.connect(self.first.root / 'inbox.sqlite3')) as db:
                db.execute('UPDATE settings SET inbox_id=? WHERE singleton=1', (self.first.inbox_id,))
                db.commit()
        process = start_service(command, recovered / 'memory-service.json')
        self.assertEqual(len(ServiceClient(config).protected_inboxes(owner)['historical']), 1)
        process.terminate(); process.wait(timeout=15)
        binding_path = recovered / 'protected-multi-binding.json'
        binding = binding_path.read_bytes()
        binding_path.write_bytes(binding.replace(b'"authentication":"', b'"authentication":"0', 1))
        try:
            with runtime._connection() as core, self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                read_state(runtime, core)
        finally:
            binding_path.write_bytes(binding)
        binding_path.unlink()
        try:
            with runtime._connection() as core, self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                read_state(runtime, core)
        finally:
            binding_path.write_bytes(binding)
        config_bytes = config.read_bytes()
        changed_config = _load(config)
        del changed_config['protected_multi_binding']
        _atomic_json(config, changed_config)
        try:
            with runtime._connection() as core, self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                read_state(runtime, core)
        finally:
            config.write_bytes(config_bytes)
        with runtime._connection() as core:
            original_auth = core.execute('SELECT authentication FROM restored_multi_attachment_v1 WHERE id=1').fetchone()['authentication']
            core.execute('UPDATE restored_multi_attachment_v1 SET authentication=? WHERE id=1', ('0' * 64,))
        try:
            with runtime._connection() as core, self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                read_state(runtime, core)
        finally:
            with runtime._connection() as core:
                core.execute('UPDATE restored_multi_attachment_v1 SET authentication=? WHERE id=1', (original_auth,))
        from evernook.archive import capture_control_checkpoint
        from evernook.runtime_restore import publish_replayed_runtime
        checkpoint = capture_control_checkpoint(runtime, owner)
        archive = self.root / 'multi-marker-archive'
        runtime.export_archive(owner, archive)
        with self.assertRaisesRegex(MemoryFailure, 'protected_restore_required'):
            publish_replayed_runtime(runtime, owner, archive, self.root / 'unsafe-republish',
                                     checkpoint=checkpoint, client_id='synthetic', entry='synthetic')
        self.assertFalse((self.root / 'unsafe-republish').exists())
        subsequent = self.root / 'subsequent-backup'
        create_backup(runtime, owner, subsequent, self.root / 'backup-keys')
        second = self.root / 'second-recovered'
        with OfflineRecovery(subsequent, second, Path(keys.recovery_file), b'synthetic multi backup password') as review:
            review.publish(ACKNOWLEDGEMENT)
        second_config, second_owner_file = second / 'runtime.json', second / 'mcp-access-owner.json'
        second_runtime = MemoryRuntime(second_config, service_owner=True)
        with second_runtime._connection() as core:
            self.assertIsNone(multi_attachment(second_runtime, core))
        process = start_service([sys.executable, '-B', '-m', 'evernook.service', '--config', str(second_config),
                                 '--activate-owner', str(second_owner_file), '--activate-restored-without-vault'],
                                second / 'memory-service.json')
        second_owner = second_runtime.access_from_file(second_owner_file)
        self.assertEqual(ServiceClient(second_config).call(second_owner, 'status', {})['service']['protected_state'], 'restored_missing')
        process.terminate(); process.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
