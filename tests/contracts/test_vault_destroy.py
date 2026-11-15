from __future__ import annotations

from contextlib import closing
from pathlib import Path

import sqlite3
import unittest
from unittest.mock import patch

from evernook import Access, MemoryFailure, MemoryRuntime
from evernook.missing_vault import read_state
from evernook.runtime import _json, _load
from evernook.vault_destroy import _load_record, _record_path, destroy_vault
from evernook.vault_multi_binding import read as read_multi
from evernook.vault_single_binding import read as read_single
from tests.contracts.test_vault_multi_restore import MultiVaultRestoreTests


class VaultDestroyTests(MultiVaultRestoreTests):
    def test_destroy_refuses_unbound_wrong_password_and_unknown_content(self) -> None:
        runtime = MemoryRuntime(self.config)
        inbox_root, key_root = self.first.root, self.first.key_root
        # Before any activation the runtime has no durable binding at all.
        with self.assertRaisesRegex(MemoryFailure, 'vault_destroy_unbound'):
            destroy_vault(self.config, self.owner, inbox_root, key_root, self.passwords[0])
        self.assertTrue((key_root / 'recovery.json').exists())
        client = self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        unknown_inbox, unknown_keys = self.root / 'unknown-inbox', self.root / 'unknown-keys'
        with self.assertRaisesRegex(MemoryFailure, 'vault_destroy_unbound'):
            destroy_vault(self.config, self.owner, unknown_inbox, unknown_keys, self.passwords[0])
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized|permission_denied'):
            destroy_vault(self.config, Access('unauthorized', 'unauthorized'), inbox_root, key_root,
                          self.passwords[0])
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            destroy_vault(self.config, self.owner, inbox_root, key_root, b'synthetic wrong password')
        self.assertTrue((key_root / 'recovery.json').exists())
        # Unknown extra content is refused without deleting anything.
        stray = inbox_root / 'stray.txt'
        stray.write_text('keep', encoding='utf-8')
        try:
            with self.assertRaisesRegex(MemoryFailure, 'vault_destroy_unknown_content'):
                destroy_vault(self.config, self.owner, inbox_root, key_root, self.passwords[0])
            self.assertTrue(stray.exists())
        finally:
            stray.unlink()

    def test_destroy_last_multi_vault_returns_to_readable_local_memory(self) -> None:
        client = self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        slots = {self.first.inbox_id: (self.first.root, self.first.key_root, self.passwords[0]),
                 self.second.inbox_id: (self.second.root, self.second.key_root, self.passwords[1])}
        result = destroy_vault(self.config, self.owner, self.first.root, self.first.key_root, self.passwords[0])
        self.assertFalse(result['physical_erasure_verified'])
        self.assertEqual(result['steps']['release_binding'], 'done')
        # The surviving vault degraded to the authenticated single binding.
        runtime = MemoryRuntime(self.config, service_owner=True)
        self.assertIsNone(read_multi(runtime))
        single = read_single(runtime)
        assert single is not None
        self.assertEqual(single['inbox_id'], self.second.inbox_id)
        config_value = _load(self.config)
        self.assertIs(config_value.get('protected_single_binding'), True)
        self.assertNotIn('protected_multi_binding', config_value)
        self.assertFalse((self.first.root / 'inbox.sqlite3').exists())
        self.assertFalse((self.first.key_root / 'recovery.json').exists())
        self.assertFalse(self.first.root.exists())
        # Destroying the surviving vault returns the runtime to plain local mode.
        result = destroy_vault(self.config, self.owner, self.second.root, self.second.key_root, self.passwords[1])
        self.assertEqual(result['steps']['release_binding'], 'done')
        self.assertIsNone(read_multi(runtime))
        self.assertIsNone(read_single(runtime))
        config_value = _load(self.config)
        self.assertEqual(set(config_value), {'schema_version', 'data_dir', 'busy_timeout_ms'})
        self.assertFalse(self.second.key_root.exists())
        # Ordinary memory stays readable in local mode; protected decryption is gone.
        local = MemoryRuntime(self.config)
        with local._connection() as db:
            self.assertGreaterEqual(int(db.execute('SELECT count(*) FROM messages').fetchone()[0]), 0)
        with self.assertRaisesRegex(MemoryFailure, 'vault_destroy_unbound'):
            destroy_vault(self.config, self.owner, slots[self.second.inbox_id][0],
                          slots[self.second.inbox_id][1], self.passwords[1])

    def test_destroy_record_is_authenticated_and_resume_skips_password(self) -> None:
        client = self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        runtime = MemoryRuntime(self.config, service_owner=True)
        original_delete = __import__('evernook.vault_destroy', fromlist=['_delete_contents'])._delete_contents
        calls = [0]
        def crash_after_keys(directory: Path, allowed: tuple[str, ...]) -> None:
            calls[0] += 1
            if calls[0] == 2:
                raise MemoryFailure('synthetic-crash-mid-destruction')
            original_delete(directory, allowed)
        with patch('evernook.vault_destroy._delete_contents', side_effect=crash_after_keys):
            with self.assertRaisesRegex(MemoryFailure, 'synthetic-crash-mid-destruction'):
                destroy_vault(self.config, self.owner, self.first.root, self.first.key_root, self.passwords[0])
        # The crash happened mid-destruction: the inbox is gone, keys remain.
        self.assertFalse((self.first.root / 'inbox.sqlite3').exists())
        self.assertTrue((self.first.key_root / 'recovery.json').exists())
        record = _load_record(runtime, self.first.inbox_id)
        assert record is not None
        self.assertEqual(record['steps']['delete_inbox'], 'done')
        # A tampered record fails closed before anything else happens.
        path = _record_path(runtime, self.first.inbox_id)
        saved = path.read_bytes()
        path.write_bytes(saved.replace(b'"done"', b'"pending"', 1))
        with self.assertRaisesRegex(MemoryFailure, 'vault_destruction_record_invalid'):
            destroy_vault(self.config, self.owner, self.first.root, self.first.key_root, b'irrelevant-now')
        self.assertTrue((self.first.key_root / 'recovery.json').exists())
        path.write_bytes(saved)
        # Resume completes without demanding the password again.
        result = destroy_vault(self.config, self.owner, self.first.root, self.first.key_root, b'irrelevant-now')
        self.assertEqual(result['steps']['delete_keys'], 'done')
        self.assertFalse(self.first.key_root.exists())


if __name__ == '__main__':
    unittest.main()
