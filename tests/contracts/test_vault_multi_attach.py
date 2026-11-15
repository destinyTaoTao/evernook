from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from typing import Callable
from unittest.mock import patch

from evernook import Access, Change, Event, MemoryFailure, MemoryRuntime, Source, pair_client
from evernook.backup import create_backup, initialize_backup_keys
from evernook.missing_vault import read_state
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.protected import ProtectedInbox
from evernook.runtime import _atomic_json, _json, _load
from evernook.service import ServiceClient
from evernook.vault_local import LocalDisplayRequest
from evernook.vault_multi_attach import _material_digests, attach_stage
from evernook.vault_multi_restore import restore_stage
from evernook.vault_multi_binding import read as read_binding
from tests.contracts.test_vault_multi_restore import MultiVaultRestoreTests


class MultiVaultAttachTests(MultiVaultRestoreTests):
    def test_destroy_recovered_member_preserves_single_attachment_and_remaining_service(self) -> None:
        from evernook.missing_vault import attachment
        from evernook.vault_destroy import destroy_vault
        from evernook.vault_retirement import retired
        config, owner, recovery_file, vault_backup = self._prepare_recovery()
        stage = self._stage(config, owner, recovery_file, vault_backup)
        attach_stage(config, owner, stage, recovery_file, b'synthetic backup password',
                     self._password(), self.root / 'attached-destroy')
        runtime = MemoryRuntime(config, service_owner=True)
        binding = read_binding(runtime)
        assert binding is not None
        first = next(item for item in binding['inboxes'] if item['inbox_id'] == self.first.inbox_id)
        second = next(item for item in binding['inboxes'] if item['inbox_id'] == self.second.inbox_id)
        destroy_vault(config, owner, Path(first['inbox_dir']), Path(first['key_dir']), self.passwords[0])
        with runtime._connection() as core:
            self.assertFalse(read_state(runtime, core))
            single = attachment(runtime, core)
            assert single is not None
            self.assertEqual(single['inbox_id'], self.second.inbox_id)
            self.assertIn(self.first.inbox_id, retired(runtime, core))
            # Original associations survive for auditing, including the lost vault.
            self.assertEqual(core.execute('SELECT count(*) FROM protected_inbox_registry_v1').fetchone()[0], 2)
        endpoint = config.parent / 'memory-service.json'
        old = endpoint.read_bytes()
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                                    '--protected-inbox', second['inbox_dir'], '--protected-keys', second['key_dir']],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if process.poll() is not None:
                assert process.stderr is not None
                self.fail(process.stderr.read().decode(errors='replace'))
            if time.monotonic() >= deadline:
                self.fail('restored remaining service startup timeout')
            time.sleep(.05)
        client = ServiceClient(config)
        self.assertEqual(client.protected_inboxes(owner)['primary'], self.second.inbox_id)
        self.assertEqual(len(client.protected_inventory(owner)['objects']), 2)
        process.terminate(); process.wait(timeout=15)
        destroy_vault(config, owner, Path(second['inbox_dir']), Path(second['key_dir']), self.passwords[1])
        local = MemoryRuntime(config)
        self.assertIsNone(local.read(self.entry_access, self.linked.message.message_id))  # type: ignore[union-attr]

    def _prepare_recovery(self) -> tuple[Path, Access, Path, Path]:
        """Activated multi service, backups, offline ordinary recovery, restored gate."""
        client = self.start(activate=True)
        keys = initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        ordinary_backup, vault_backup = self.root / 'ordinary-backup', self.root / 'vault-backup'
        create_backup(MemoryRuntime(self.config, service_owner=True), self.owner, ordinary_backup, self.root / 'backup-keys')
        client.protected_backup(self.owner, vault_backup, self.root / 'backup-keys')
        client.protected_receive(self.owner, replace(self.event, source=replace(self.event.source, event='after-backup')))
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
        self.entry_access = entry_access
        return config, owner, Path(keys.recovery_file), vault_backup

    def _stage(self, config: Path, owner: Access, recovery_file: Path, vault_backup: Path) -> Path:
        by_id = {self.first.inbox_id: self.passwords[0], self.second.inbox_id: self.passwords[1]}
        def password(_slot: str, inbox_id: str) -> bytes:
            return by_id[inbox_id]
        destination = self.root / 'staged-set'
        result = restore_stage(config, owner, vault_backup, destination, recovery_file,
                               b'synthetic backup password', password)
        self.assertEqual(result.inboxes, 2)
        return destination

    def _password(self) -> Callable[[str, str], bytes]:
        by_id = {self.first.inbox_id: self.passwords[0], self.second.inbox_id: self.passwords[1]}
        def password(_slot: str, inbox_id: str) -> bytes:
            return by_id[inbox_id]
        return password

    def test_attach_publishes_set_gate_falls_only_at_commit_and_service_reads_it(self) -> None:
        config, owner, recovery_file, vault_backup = self._prepare_recovery()
        destination = self._stage(config, owner, recovery_file, vault_backup)
        password = self._password()
        destinations = self.root / 'attached'
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized|permission_denied'):
            attach_stage(config, Access('unauthorized', 'unauthorized'), destination, recovery_file,
                         b'synthetic backup password', password, destinations)
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            attach_stage(config, owner, destination, recovery_file, b'synthetic backup password',
                         lambda *_: b'synthetic wrong vault password', destinations)
        self.assertFalse(destinations.exists())
        gate_runtime = MemoryRuntime(config, service_owner=True)
        with gate_runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            self.assertTrue(read_state(gate_runtime, core))
        result = attach_stage(config, owner, destination, recovery_file,
                              b'synthetic backup password', password, destinations)
        self.assertEqual(result.inboxes, 2)
        self.assertEqual(result.protected_objects, 3)
        self.assertFalse(result.daily_admitted)
        self.assertEqual([Path(item).name for item in result.destinations], ['00', '01'])
        # The gate fell exactly at commit: no missing state, authenticated marker.
        restored_runtime = MemoryRuntime(config, service_owner=True)
        with restored_runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            self.assertFalse(read_state(restored_runtime, core))
            rows = core.execute('SELECT content,authentication FROM restored_multi_attachment_v1').fetchall()
            self.assertEqual(len(rows), 1)
        binding = read_binding(restored_runtime)
        assert binding is not None
        self.assertEqual({item['inbox_id'] for item in binding['inboxes']},
                         {self.first.inbox_id, self.second.inbox_id})
        self.assertEqual(binding['primary'], self.first.inbox_id)
        config_value = _load(config)
        self.assertIs(config_value.get('protected_multi_binding'), True)
        self.assertIs(config_value.get('restored_missing_vault'), True)
        self.assertIs(config_value.get('service_mode'), True)
        # Replay is refused: the missing gate is gone.
        with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
            attach_stage(config, owner, destination, recovery_file, b'synthetic backup password',
                         password, self.root / 'again')
        # The attached service reads both vaults end to end and decrypts locally.
        # Backup slots are ordered by inbox_id, so the operator derives the
        # primary destination from the persisted binding instead of slot order.
        binding = read_binding(MemoryRuntime(config, service_owner=True))
        assert binding is not None
        primary_entry = next(item for item in binding['inboxes'] if item['inbox_id'] == binding['primary'])
        history_entry = next(item for item in binding['inboxes'] if item['inbox_id'] != binding['primary'])
        endpoint = config.parent / 'memory-service.json'
        old_endpoint = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                                    '--protected-inbox', primary_entry['inbox_dir'],
                                    '--protected-keys', primary_entry['key_dir'],
                                    '--protected-history-inbox', history_entry['inbox_dir'],
                                    '--protected-history-keys', history_entry['key_dir']],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == old_endpoint:
            if process.poll() is not None:
                assert process.stderr is not None
                self.fail('attached service exited: ' + process.stderr.read().decode(errors='replace'))
            if time.monotonic() >= deadline:
                self.fail('attached service startup timeout')
            time.sleep(.05)
        client = ServiceClient(config)
        status = client.call(owner, 'status', {})['service']
        self.assertEqual(status['protected_state'], 'configured')
        self.assertTrue(status['single_writer'])
        self.assertEqual(client.protected_inboxes(owner),
                         {'primary': self.first.inbox_id, 'historical': [self.second.inbox_id]})
        self.assertEqual(len(client.protected_inventory(owner)['objects']), 1)
        self.assertEqual(len(client.protected_inventory(owner, inbox_id=self.second.inbox_id)['objects']), 2)
        attached_runtime = MemoryRuntime(config, service_owner=True)
        primary = ProtectedInbox(attached_runtime, Path(primary_entry['inbox_dir']), Path(primary_entry['key_dir']))
        history = ProtectedInbox(attached_runtime, Path(history_entry['inbox_dir']), Path(history_entry['key_dir']))
        seen: list[str] = []
        # Re-paired entry client: the recovered administrator entry is not the
        # original source entry, so display authorization needs explicit pairing.
        # The primary's linked original was suppressed after recovery, so the
        # unsuppressed historical object proves local decryption end to end.
        LocalDisplayRequest(history, self.entry_access, self.pending.object_id, 'synthetic-attach',
                            desktop=lambda: True).display_once(self.passwords[1], seen.append)
        self.assertEqual(seen, [self.event.text])
        process.terminate(); process.wait(timeout=15)

    def test_attach_crash_before_commit_keeps_gate_and_retry_completes(self) -> None:
        config, owner, recovery_file, vault_backup = self._prepare_recovery()
        destination = self._stage(config, owner, recovery_file, vault_backup)
        password = self._password()
        destinations = self.root / 'attached'
        original_atomic = _atomic_json
        flipped: list[Path] = []

        def crash_after_config_flip(path: Path, value: object) -> None:
            original_atomic(path, value)
            config_value = _load(config)
            if config_value.get('protected_multi_binding') is True:
                flipped.append(path)
                raise MemoryFailure('synthetic-crash-after-publication')
        with patch('evernook.vault_multi_attach._atomic_json', side_effect=crash_after_config_flip):
            with self.assertRaisesRegex(MemoryFailure, 'synthetic-crash-after-publication'):
                attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        self.assertTrue(flipped)
        restored_runtime = MemoryRuntime(config, service_owner=True)
        with restored_runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            self.assertTrue(read_state(restored_runtime, core))
        result = attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        self.assertEqual(result.inboxes, 2)
        with restored_runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            self.assertFalse(read_state(restored_runtime, core))

    def test_attach_faults_fail_closed_and_partial_publication_retries(self) -> None:
        config, owner, recovery_file, vault_backup = self._prepare_recovery()
        destination = self._stage(config, owner, recovery_file, vault_backup)
        password = self._password()
        destinations = self.root / 'attached'
        saved = destination.read_bytes()
        tampered = bytearray(saved)
        import struct as _struct
        import zipfile as _zipfile
        with _zipfile.ZipFile(destination) as archive:
            info = archive.getinfo('01-inbox.enc')
            header = saved[info.header_offset:info.header_offset + 30]
            (_sig, _ver, _flag, _method, _time, _date, _crc, _csize, _usize,
             name_len, extra_len) = _struct.unpack('<4sHHHHHIIIHH', header)
            data_offset = info.header_offset + 30 + name_len + extra_len
        # ZIP64 entries carry sentinel sizes in the local header; the central
        # directory's file_size is authoritative for the stored data extent.
        tampered[data_offset + info.file_size - 1] ^= 1
        destination.write_bytes(bytes(tampered))
        try:
            with self.assertRaises(MemoryFailure):
                attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
            self.assertFalse(destinations.exists())
        finally:
            destination.write_bytes(saved)
        # A crashed publication leaves one published slot; the retry verifies
        # and reuses it instead of trusting it blindly.
        published_calls = [0]
        def crash_after_first_slot(key_root: Path, fingerprint_key: bytes) -> list[str]:
            published_calls[0] += 1
            if published_calls[0] > 1:
                raise MemoryFailure('synthetic-crash-mid-publication')
            return _material_digests(key_root, fingerprint_key)
        with patch('evernook.vault_multi_attach._material_digests', side_effect=crash_after_first_slot):
            with self.assertRaisesRegex(MemoryFailure, 'synthetic-crash-mid-publication'):
                attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        self.assertTrue((destinations / '00' / 'inbox' / 'inbox.sqlite3').is_file())
        # A tampered published key file is refused when the destination is reused.
        tampered_key = (destinations / '00' / 'keys' / 'writer.key').read_bytes()
        (destinations / '00' / 'keys' / 'writer.key').write_bytes(b'tampered-key')
        with self.assertRaisesRegex(MemoryFailure, 'vault_attach_destination_mismatch'):
            attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        (destinations / '00' / 'keys' / 'writer.key').write_bytes(tampered_key)
        # A ledger tamper between staging and attach breaks two-direction revalidation.
        runtime = MemoryRuntime(config, service_owner=True)
        with runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            row = core.execute('SELECT reference FROM protected_links_v1 LIMIT 1').fetchone()
            reference = json.loads(row['reference'])
            reference['envelope_sha256'] = '0' * 64
            core.execute('UPDATE protected_links_v1 SET reference=? WHERE reference=?', (_json(reference), row['reference']))
            core.execute('COMMIT')
        with self.assertRaisesRegex(MemoryFailure, 'vault_backup_ledger_mismatch|vault_attach_failed'):
            attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        with runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            core.execute('UPDATE protected_links_v1 SET reference=? WHERE reference=?',
                         (row['reference'], _json(reference)))
            core.execute('COMMIT')
        # Slot 01 may be partially copied when the crash lands mid-publication;
        # the retry revalidates every published byte instead of trusting it.
        result = attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)
        self.assertEqual(result.inboxes, 2)
        self.assertTrue((destinations / '01' / 'inbox' / 'inbox.sqlite3').is_file())
        # A committed attach cannot be replayed.
        with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
            attach_stage(config, owner, destination, recovery_file, b'synthetic backup password', password, destinations)


if __name__ == '__main__':
    unittest.main()
