from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Callable
from contextlib import closing
from unittest.mock import patch

from evernook import Access, MemoryFailure, MemoryRuntime, initialize, pair_client
from evernook.paths import verify_private_directory
from evernook.runtime import _atomic_json, _load
from evernook.vault_multi_binding import _auth
from evernook.vault_workspace import cleanup_workspaces, list_workspaces, secure_workspace


class SecureWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-secure-workspace-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.owner_file = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_file)
        runtime = MemoryRuntime(self.config, service_owner=True)
        binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
            {'inbox_id': 'synthetic-a', 'key_id': 'key-a', 'inbox_dir': str(self.root / 'outside-a'),
             'key_dir': str(self.root / 'outside-keys-a'), 'material': ['a', 'b', 'c']},
            {'inbox_id': 'synthetic-b', 'key_id': 'key-b', 'inbox_dir': str(self.root / 'outside-b'),
             'key_dir': str(self.root / 'outside-keys-b'), 'material': ['d', 'e', 'f']}]}
        _atomic_json(runtime.root / 'protected-multi-binding.json',
                     {'content': binding, 'authentication': _auth(runtime, binding)})
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'protected_multi_binding': True})

    def run_locked(self, body: Callable[[MemoryRuntime, sqlite3.Connection], Any]) -> Any:
        """Run a body inside the stopped-service exclusive gate like real flows."""
        runtime = MemoryRuntime(self.config, service_owner=True)
        from evernook.vault_restore import _stopped_service
        with _stopped_service(runtime.root):
            with runtime._connection(ownership_exclusive=True) as core:
                core.execute('BEGIN IMMEDIATE')
                result = body(runtime, core)
                core.execute('COMMIT')
                return result

    def registered_paths(self, runtime: MemoryRuntime) -> list[str]:
        from evernook.vault_workspace import _read
        return [entry['path'] for entry in _read(runtime)]

    def crash(self) -> dict[str, object]:
        worker = Path(__file__).with_name('secure_workspace_worker.py')
        result = subprocess.run([sys.executable, str(worker), str(self.root)], capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 91, result.stderr.decode(errors='replace'))
        entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_secure_workspace_is_born_private_and_cleaned_as_one_unit(self) -> None:
        def body(runtime: MemoryRuntime, core: sqlite3.Connection) -> tuple[str, Path]:
            with secure_workspace(runtime, self.root, 'multi', core=core) as temporary:
                registered = self.registered_paths(runtime)
                self.assertEqual(len(registered), 1)
                vaults = temporary.mkdir('vaults')
                slot = vaults.mkdir('03')
                inbox = slot.mkdir('inbox')
                keys = slot.mkdir('keys')
                db = inbox.create_file('inbox.sqlite3', write_share=True)
                verify_private_directory(inbox.path, extra_files=(inbox.path / 'inbox.sqlite3',))
                with closing(sqlite3.connect(str(inbox.path / 'inbox.sqlite3'))) as check:
                    check.execute('PRAGMA journal_mode=WAL')
                    check.execute('CREATE TABLE t (x)')
                    check.execute("INSERT INTO t VALUES ('synthetic')")
                    check.commit()
                with closing(sqlite3.connect(str(inbox.path / 'inbox.sqlite3'))) as check:
                    self.assertEqual(check.execute('SELECT x FROM t').fetchone()[0], 'synthetic')
                key = keys.create_file('recovery.json')
                key.stream.write(b'synthetic-key')
                key.stream.flush()
                return registered[0], inbox.path
        path, inbox_path = self.run_locked(body)
        runtime = MemoryRuntime(self.config, service_owner=True)
        self.assertEqual(self.registered_paths(runtime), [])
        self.assertFalse(Path(path).exists())
        self.assertFalse(inbox_path.exists())

    def test_registration_failure_leaves_no_residue(self) -> None:
        observed: list[Path] = []
        def fake_save(runtime: MemoryRuntime, entries: list[dict[str, object]]) -> None:
            observed.append(Path(str(entries[-1]['path'])))
            raise MemoryFailure('synthetic-save-failure')
        def body(runtime: MemoryRuntime, core: sqlite3.Connection) -> None:
            with patch('evernook.vault_workspace._save', side_effect=fake_save):
                with self.assertRaisesRegex(MemoryFailure, 'synthetic-save-failure'):
                    with secure_workspace(runtime, self.root, 'multi', core=core) as temporary:
                        temporary.mkdir('vaults')
        self.run_locked(body)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        runtime = MemoryRuntime(self.config, service_owner=True)
        self.assertEqual(self.registered_paths(runtime), [])

    def test_verification_failure_discards_material(self) -> None:
        observed: list[str] = []
        def body(runtime: MemoryRuntime, core: sqlite3.Connection) -> None:
            with secure_workspace(runtime, self.root, 'multi', core=core) as temporary:
                observed.append(self.registered_paths(runtime)[0])
                vaults = temporary.mkdir('vaults')
                slot = vaults.mkdir('00')
                inbox = slot.mkdir('inbox')
                db = inbox.create_file('inbox.sqlite3', write_share=True)
                db.stream.write(b'partial')
                db.stream.flush()
                raise ValueError('synthetic')
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            self.run_locked(body)
        self.assertEqual(len(observed), 1)
        self.assertFalse(Path(observed[0]).exists())
        runtime = MemoryRuntime(self.config, service_owner=True)
        self.assertEqual(self.registered_paths(runtime), [])

    def test_crashed_secure_workspace_is_registered_and_cleanable(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        self.assertEqual(entry['kind'], 'multi')
        self.assertTrue((path / 'vaults' / '00' / 'inbox' / 'inbox.sqlite3').exists())
        self.assertTrue((path / 'ledger' / 'idempotency.key').exists())
        verify_private_directory(path)
        result = cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(result['removed'], 1)
        self.assertFalse(path.exists())
        self.assertEqual(list_workspaces(self.config, self.owner), [])

    def test_crashed_secure_workspace_refuses_unknown_content(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        unknown = path / 'vaults' / '16'
        unknown.mkdir()
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unknown_content'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        unknown.rmdir()
        self.assertTrue((path / 'vaults' / '15' / 'keys' / 'recovery.json').exists())
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])


if __name__ == '__main__':
    unittest.main()
