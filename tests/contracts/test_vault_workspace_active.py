from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evernook import MemoryFailure, MemoryRuntime, initialize, pair_client
from evernook.runtime import _atomic_json, _json, _load
from evernook.vault_multi_binding import _auth
from evernook.vault_restore import _stopped_service
from evernook.vault_workspace import candidate_workspace, cleanup_workspaces, list_workspaces, workspace


class ActiveWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-active-workspace-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.config.parent / 'mcp-access-owner.json')
        self.runtime = MemoryRuntime(self.config, service_owner=True)

    def candidates(self) -> tuple[Path, Path, str, str]:
        with _stopped_service(self.runtime.root), self.runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            with candidate_workspace(self.runtime, self.root) as active:
                pass
            with candidate_workspace(self.runtime, self.root) as unused:
                pass
            core.execute('COMMIT')
        entries = list_workspaces(self.config, self.owner)
        by_path = {item['path']: item['id'] for item in entries}
        return active, unused, str(by_path[str(active)]), str(by_path[str(unused)])

    def check_mixed_selection(self, active: Path, unused: Path, active_id: str, unused_id: str,
                              table: str) -> None:
        with patch('evernook.missing_vault._receipt', return_value='synthetic-receipt'):
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_active'):
                cleanup_workspaces(self.config, self.owner, [unused_id, active_id])
            self.assertTrue(active.exists())
            self.assertTrue(unused.exists())
            with self.runtime._connection() as core:
                original = core.execute(f'SELECT authentication FROM {table} WHERE id=1').fetchone()['authentication']
                core.execute(f'UPDATE {table} SET authentication=? WHERE id=1', ('0' * 64,))
            try:
                with self.assertRaisesRegex(MemoryFailure, 'protected_recovery_proof_invalid'):
                    cleanup_workspaces(self.config, self.owner, [unused_id])
                self.assertTrue(unused.exists())
            finally:
                with self.runtime._connection() as core:
                    core.execute(f'UPDATE {table} SET authentication=? WHERE id=1', (original,))
            self.assertEqual(cleanup_workspaces(self.config, self.owner, [unused_id])['removed'], 1)
            self.assertTrue(active.exists())

    def test_active_single_attachment_blocks_cleanup(self) -> None:
        active, unused, active_id, unused_id = self.candidates()
        content = {'version': 1, 'receipt_auth': 'synthetic-receipt', 'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
                   'key_dir': str(active / 'vaults' / '00' / 'keys'), 'inbox_id': 'synthetic', 'key_id': 'synthetic',
                   'material': ['a', 'b', 'c']}
        signature = hmac.digest(self.runtime._fingerprint_key, b'evernook-vault-attachment-v1\0'
                                + _json(content).encode(), hashlib.sha256).hex()
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'restored_missing_vault': True})
        with self.runtime._connection() as core:
            core.execute('CREATE TABLE restored_vault_attachment_v1 (id INTEGER PRIMARY KEY, content TEXT NOT NULL, authentication TEXT NOT NULL)')
            core.execute('INSERT INTO restored_vault_attachment_v1 VALUES (1,?,?)', (_json(content), signature))
        self.check_mixed_selection(active, unused, active_id, unused_id, 'restored_vault_attachment_v1')

    def test_active_multi_attachment_blocks_cleanup(self) -> None:
        active, unused, active_id, unused_id = self.candidates()
        binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
            {'inbox_id': 'synthetic-a', 'key_id': 'synthetic-key-a', 'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
             'key_dir': str(active / 'vaults' / '00' / 'keys'), 'material': ['a', 'b', 'c']},
            {'inbox_id': 'synthetic-b', 'key_id': 'synthetic-key-b', 'inbox_dir': str(self.root / 'other-inbox'),
             'key_dir': str(self.root / 'other-keys'), 'material': ['d', 'e', 'f']}]}
        _atomic_json(self.runtime.root / 'protected-multi-binding.json', {'content': binding, 'authentication': _auth(self.runtime, binding)})
        content = {'version': 1, 'receipt_auth': 'synthetic-receipt', 'binding': binding}
        signature = hmac.digest(self.runtime._fingerprint_key, b'evernook-multi-vault-attachment-v1\0'
                                + _json(content).encode(), hashlib.sha256).hex()
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'restored_missing_vault': True, 'protected_multi_binding': True})
        with self.runtime._connection() as core:
            core.execute('CREATE TABLE restored_multi_attachment_v1 (id INTEGER PRIMARY KEY, content TEXT NOT NULL, authentication TEXT NOT NULL)')
            core.execute('INSERT INTO restored_multi_attachment_v1 VALUES (1,?,?)', (_json(content), signature))
        self.check_mixed_selection(active, unused, active_id, unused_id, 'restored_multi_attachment_v1')

    def test_non_recovered_multi_binding_also_blocks_cleanup(self) -> None:
        active, unused, active_id, unused_id = self.candidates()
        binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
            {'inbox_id': 'synthetic-a', 'key_id': 'synthetic-key-a', 'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
             'key_dir': str(active / 'vaults' / '00' / 'keys'), 'material': ['a', 'b', 'c']},
            {'inbox_id': 'synthetic-b', 'key_id': 'synthetic-key-b', 'inbox_dir': str(self.root / 'other-inbox'),
             'key_dir': str(self.root / 'other-keys'), 'material': ['d', 'e', 'f']}]}
        _atomic_json(self.runtime.root / 'protected-multi-binding.json', {'content': binding, 'authentication': _auth(self.runtime, binding)})
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'protected_multi_binding': True})
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_active'):
            cleanup_workspaces(self.config, self.owner, [unused_id, active_id])
        self.assertTrue(active.exists())
        self.assertTrue(unused.exists())
        self.assertEqual(cleanup_workspaces(self.config, self.owner, [unused_id])['removed'], 1)

    def test_legacy_single_service_with_unbound_paths_refuses_cleanup(self) -> None:
        active, unused, active_id, unused_id = self.candidates()
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unbound_active'):
            cleanup_workspaces(self.config, self.owner, [unused_id, active_id])
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True})
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unbound_active'):
            cleanup_workspaces(self.config, self.owner, [unused_id, active_id])
        original = _load(self.config)
        del original['service_mode']
        _atomic_json(self.config, original)
        with self.runtime._connection() as core:
            core.execute('CREATE TABLE protected_inbox_registry_v1 (inbox_id TEXT PRIMARY KEY, key_id TEXT NOT NULL)')
            core.execute('INSERT INTO protected_inbox_registry_v1 VALUES (?,?)', ('synthetic', 'synthetic-key'))
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unbound_active'):
            cleanup_workspaces(self.config, self.owner, [unused_id, active_id])
        self.assertTrue(active.exists())
        self.assertTrue(unused.exists())

    def test_exception_after_candidate_attachment_keeps_active_candidate(self) -> None:
        with patch('evernook.missing_vault._receipt', return_value='synthetic-receipt'):
            with _stopped_service(self.runtime.root), self.runtime._connection(ownership_exclusive=True) as core:
                core.execute('BEGIN IMMEDIATE')
                with self.assertRaisesRegex(RuntimeError, 'synthetic-after-commit'):
                    with candidate_workspace(self.runtime, self.root) as active:
                        content = {'version': 1, 'receipt_auth': 'synthetic-receipt',
                                   'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
                                   'key_dir': str(active / 'vaults' / '00' / 'keys'),
                                   'inbox_id': 'synthetic', 'key_id': 'synthetic', 'material': ['a', 'b', 'c']}
                        signature = hmac.digest(self.runtime._fingerprint_key, b'evernook-vault-attachment-v1\0'
                                                + _json(content).encode(), hashlib.sha256).hex()
                        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'restored_missing_vault': True})
                        core.execute('CREATE TABLE restored_vault_attachment_v1 (id INTEGER PRIMARY KEY, content TEXT NOT NULL, authentication TEXT NOT NULL)')
                        core.execute('INSERT INTO restored_vault_attachment_v1 VALUES (1,?,?)', (_json(content), signature))
                        core.execute('COMMIT')
                        raise RuntimeError('synthetic-after-commit')
            self.assertTrue(active.exists())
            entries = list_workspaces(self.config, self.owner)
            self.assertEqual(len(entries), 1)
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_active'):
                cleanup_workspaces(self.config, self.owner, [entries[0]['id']])

    def test_automatic_cleanup_refuses_a_newly_active_workspace(self) -> None:
        with _stopped_service(self.runtime.root), self.runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_active'):
                with workspace(self.runtime, self.root, 'multi', core=core) as active:
                    binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
                        {'inbox_id': 'synthetic-a', 'key_id': 'key-a', 'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
                         'key_dir': str(active / 'vaults' / '00' / 'keys'), 'material': ['a', 'b', 'c']},
                        {'inbox_id': 'synthetic-b', 'key_id': 'key-b', 'inbox_dir': str(self.root / 'other-inbox'),
                         'key_dir': str(self.root / 'other-keys'), 'material': ['d', 'e', 'f']}]}
                    _atomic_json(self.runtime.root / 'protected-multi-binding.json', {'content': binding, 'authentication': _auth(self.runtime, binding)})
                    _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'protected_multi_binding': True})
                    core.execute('COMMIT')
        self.assertTrue(active.exists())
        self.assertEqual(len(list_workspaces(self.config, self.owner)), 1)

    def test_automatic_cleanup_sees_uncommitted_attachment(self) -> None:
        original_config = self.config.read_bytes()
        with patch('evernook.missing_vault._receipt', return_value='synthetic-receipt'):
            with _stopped_service(self.runtime.root), self.runtime._connection(ownership_exclusive=True) as core:
                core.execute('BEGIN IMMEDIATE')
                with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_active'):
                    with workspace(self.runtime, self.root, 'multi', core=core) as active:
                        content = {'version': 1, 'receipt_auth': 'synthetic-receipt',
                                   'inbox_dir': str(active / 'vaults' / '00' / 'inbox'),
                                   'key_dir': str(active / 'vaults' / '00' / 'keys'),
                                   'inbox_id': 'synthetic', 'key_id': 'synthetic', 'material': ['a', 'b', 'c']}
                        signature = hmac.digest(self.runtime._fingerprint_key, b'evernook-vault-attachment-v1\0'
                                                + _json(content).encode(), hashlib.sha256).hex()
                        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'restored_missing_vault': True})
                        core.execute('CREATE TABLE restored_vault_attachment_v1 (id INTEGER PRIMARY KEY, content TEXT NOT NULL, authentication TEXT NOT NULL)')
                        core.execute('INSERT INTO restored_vault_attachment_v1 VALUES (1,?,?)', (_json(content), signature))
                core.execute('ROLLBACK')
            self.config.write_bytes(original_config)
        self.assertTrue(active.exists())
        entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(entries), 1)
        binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
            {'inbox_id': 'synthetic-a', 'key_id': 'key-a', 'inbox_dir': str(self.root / 'outside-a'),
             'key_dir': str(self.root / 'outside-keys-a'), 'material': ['a', 'b', 'c']},
            {'inbox_id': 'synthetic-b', 'key_id': 'key-b', 'inbox_dir': str(self.root / 'outside-b'),
             'key_dir': str(self.root / 'outside-keys-b'), 'material': ['d', 'e', 'f']}]}
        _atomic_json(self.runtime.root / 'protected-multi-binding.json', {'content': binding, 'authentication': _auth(self.runtime, binding)})
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'protected_multi_binding': True})
        cleanup_workspaces(self.config, self.owner, [entries[0]['id']])


if __name__ == '__main__':
    unittest.main()
