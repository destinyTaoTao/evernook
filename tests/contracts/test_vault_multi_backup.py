from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source
from evernook.backup import _decrypt, _hex, _unlock, initialize_backup_keys
from evernook.protected import ProtectedInbox
from evernook.runtime import _json
from evernook.vault_multi_backup import create_multi_backup
from evernook.backup import _encrypt
from evernook.vault_migration import _materials
from evernook.vault_workspace import cleanup_workspaces, list_workspaces
from tests.contracts.test_vault_multi_service import MultiVaultServiceTests


class MultiVaultBackupTests(MultiVaultServiceTests):
    def test_link_digest_survives_later_suppression(self) -> None:
        client = self.start(activate=True)
        keys_root = self.root / 'backup-keys'
        keys = initialize_backup_keys(keys_root, b'synthetic backup password')
        key_id, private, _sender = _unlock(Path(keys.recovery_file), b'synthetic backup password')
        def backup_digest(path: Path) -> dict[str, str]:
            result = client.protected_backup(self.owner, path, keys_root)
            content = json.loads((path / 'backup.json').read_bytes())['content']
            aad = _json({'backup_id': result['backup_id'], 'backup_key_id': key_id, 'file': 'binding'}).encode()
            output = io.BytesIO()
            _decrypt(path / 'binding.enc', output, private, content['files']['binding'], aad)
            return {item['inbox_id']: item['link_digest'] for item in json.loads(output.getvalue())['inboxes']}
        before = backup_digest(self.root / 'before-suppression')
        runtime = MemoryRuntime(self.config, service_owner=True)
        instruction = runtime.submit(self.owner, Event(Source('owner', 'synthetic', 'main', 'suppress'),
                                     'user', 5, None, 'complete', 'synthetic suppression'))
        assert self.linked.message is not None
        runtime.change(self.owner, Change('synthetic-suppress', self.linked.message.message_id, 1,
                                         'suppress', instruction.message_id, 'synthetic'))
        self.assertEqual(backup_digest(self.root / 'after-suppression'), before)

    def offline_inboxes(self) -> tuple[MemoryRuntime, ProtectedInbox, ProtectedInbox]:
        self.start(activate=True)
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        runtime = MemoryRuntime(self.config, service_owner=True)
        return runtime, ProtectedInbox(runtime, self.first.root, self.first.key_root), ProtectedInbox(runtime, self.second.root, self.second.key_root)

    def test_timeout_in_preflight_never_publishes(self) -> None:
        runtime, primary, history = self.offline_inboxes()
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        def delayed(inbox: ProtectedInbox) -> list[str]:
            time.sleep(.2)
            return _materials(inbox)
        with patch('evernook.vault_multi_verify._materials', side_effect=delayed):
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_timeout'):
                create_multi_backup(runtime, primary, (primary, history), self.owner,
                                    self.root / 'expired', self.root / 'backup-keys', timeout=.1)
        self.assertFalse((self.root / 'expired').exists())

    def test_tampered_binding_and_omitted_inbox_never_publish(self) -> None:
        runtime, primary, history = self.offline_inboxes()
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        destination = self.root / 'tampered'
        with self.assertRaises(MemoryFailure):
            create_multi_backup(runtime, primary, (primary,), self.owner, destination, self.root / 'backup-keys')
        binding = runtime.root / 'protected-multi-binding.json'
        original = binding.read_bytes()
        binding.write_bytes(original.replace(b'"authentication":"', b'"authentication":"0', 1))
        try:
            with self.assertRaisesRegex(MemoryFailure, 'protected_multi_binding_invalid'):
                create_multi_backup(runtime, primary, (primary, history), self.owner, destination, self.root / 'backup-keys')
            self.assertFalse(destination.exists())
        finally:
            binding.write_bytes(original)

    def test_canceled_encryption_cleans_registered_work_and_publishes_nothing(self) -> None:
        runtime, primary, history = self.offline_inboxes()
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        canceled = threading.Event()
        def interrupt(*args: object, **kwargs: object) -> object:
            canceled.set()
            return _encrypt(*args, **kwargs)  # type: ignore[arg-type]
        with patch('evernook.vault_multi_backup._encrypt', side_effect=interrupt):
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_canceled'):
                create_multi_backup(runtime, primary, (primary, history), self.owner,
                                    self.root / 'canceled', self.root / 'backup-keys', cancel=canceled)
        self.assertFalse((self.root / 'canceled').exists())
        self.assertEqual(list_workspaces(self.config, self.owner), [])

    def test_complete_set_lock_blocks_concurrent_private_write(self) -> None:
        runtime, primary, history = self.offline_inboxes()
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        entered, release = threading.Event(), threading.Event()
        def paused(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(20):
                raise RuntimeError('synthetic test timeout')
            return _encrypt(*args, **kwargs)  # type: ignore[arg-type]
        event = replace(self.event, source=replace(self.event.source, event='after-backup'))
        with patch('evernook.vault_multi_backup._encrypt', side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
            backup = pool.submit(create_multi_backup, runtime, primary, (primary, history), self.owner,
                                 self.root / 'locked-backup', self.root / 'backup-keys')
            try:
                self.assertTrue(entered.wait(30))
                write = pool.submit(primary.stage, self.owner, event)
                with self.assertRaises(TimeoutError):
                    write.result(timeout=.2)
            finally:
                release.set()
            self.assertFalse(backup.result(timeout=60).recovery_tested)
            self.assertEqual(write.result(timeout=30).state, 'protected_pending_link')

    def test_rename_uncertainty_keeps_published_package_and_reports_unknown(self) -> None:
        runtime, primary, history = self.offline_inboxes()
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        destination = self.root / 'uncertain'
        rename = os.rename
        def publish_then_fail(source: Path, target: Path) -> None:
            rename(source, target)
            if Path(target).resolve() == destination.resolve():
                raise OSError('synthetic failure after publication')
        with patch('evernook.vault_multi_backup.os.rename', side_effect=publish_then_fail):
            with self.assertRaisesRegex(MemoryFailure, 'vault_backup_outcome_unknown'):
                create_multi_backup(runtime, primary, (primary, history), self.owner, destination, self.root / 'backup-keys')
        self.assertTrue((destination / 'backup.json').exists())

    def test_interrupted_backup_is_registered_and_cleanable(self) -> None:
        self.start(activate=True)
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)
        initialize_backup_keys(self.root / 'backup-keys', b'synthetic backup password')
        worker = Path(__file__).with_name('vault_multi_backup_crash_worker.py')
        process = subprocess.run([sys.executable, str(worker), str(self.root)], capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 91, process.stderr.decode(errors='replace'))
        self.assertFalse((self.root / 'crash-backup').exists())
        entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'multi')
        self.assertTrue((Path(entries[0]['path']) / 'package').exists())
        self.assertEqual(cleanup_workspaces(self.config, self.owner, [entries[0]['id']])['removed'], 1)

    def test_complete_package_is_encrypted_signed_and_bound(self) -> None:
        client = self.start(activate=True)
        keys_root = self.root / 'backup-keys'
        keys = initialize_backup_keys(keys_root, b'synthetic backup password')
        destination = self.root / 'set-backup'
        with self.assertRaises(MemoryFailure):
            client.protected_backup(self.reader, destination, keys_root)
        self.assertFalse(destination.exists())
        result = client.protected_backup(self.owner, destination, keys_root)
        self.assertEqual(result['inboxes'], 2)
        self.assertFalse(result['recovery_tested'])
        self.assertFalse(result['restore_supported'])
        header = json.loads((destination / 'backup.json').read_bytes())
        content = header['content']
        key_id, private, sender = _unlock(Path(keys.recovery_file), b'synthetic backup password')
        sender.verify(_json(content).encode(), _hex(header['signature'], 64))
        self.assertEqual(content['format'], 'evernook-protected-multi-backup')
        self.assertEqual(len(content['files']), 9)
        for path in destination.iterdir():
            self.assertNotIn(self.event.text.encode(), path.read_bytes())
        def decrypt(name: str) -> bytes:
            output = io.BytesIO()
            aad = _json({'backup_id': result['backup_id'], 'backup_key_id': key_id, 'file': name}).encode()
            _decrypt(destination / (name + '.enc'), output, private, content['files'][name], aad)
            return output.getvalue()
        binding = json.loads(decrypt('binding'))
        self.assertEqual(binding['primary'], self.first.inbox_id)
        self.assertEqual({item['inbox_id'] for item in binding['inboxes']}, {self.first.inbox_id, self.second.inbox_id})
        for item in binding['inboxes']:
            self.assertTrue(decrypt(item['slot'] + '-inbox').startswith(b'SQLite format 3\x00'))
            self.assertEqual(len(item['link_digest']), 64)
        with self.assertRaisesRegex(MemoryFailure, 'backup_destination_exists'):
            client.protected_backup(self.owner, destination, keys_root)


if __name__ == '__main__':
    unittest.main()
