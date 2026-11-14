from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from evernook import Access, MemoryFailure, MemoryRuntime, initialize, pair_client
from evernook.paths import provision_private_directory, verify_private_directory
from evernook.runtime import _atomic_json, _load
from evernook.vault_multi_binding import _auth
from evernook.vault_restore import _stopped_service
from evernook.vault_workspace import candidate_workspace, cleanup_workspaces, list_workspaces


class VaultWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-workspaces-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.owner_file = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_file)
        self.base_config = self.config.read_bytes()
        runtime = MemoryRuntime(self.config, service_owner=True)
        binding = {'version': 1, 'ledger_id': 'synthetic-ledger', 'primary': 'synthetic-a', 'inboxes': [
            {'inbox_id': 'synthetic-a', 'key_id': 'key-a', 'inbox_dir': str(self.root / 'outside-a'),
             'key_dir': str(self.root / 'outside-keys-a'), 'material': ['a', 'b', 'c']},
            {'inbox_id': 'synthetic-b', 'key_id': 'key-b', 'inbox_dir': str(self.root / 'outside-b'),
             'key_dir': str(self.root / 'outside-keys-b'), 'material': ['d', 'e', 'f']}]}
        self.binding_path = runtime.root / 'protected-multi-binding.json'
        _atomic_json(self.binding_path, {'content': binding, 'authentication': _auth(runtime, binding)})
        _atomic_json(self.config, {**_load(self.config), 'service_mode': True, 'protected_multi_binding': True})
        self.process: subprocess.Popen[bytes] | None = None
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.communicate(timeout=20)
            self.process = None

    def crash(self, kind: str = 'restore') -> dict[str, object]:
        worker = Path(__file__).with_name('vault_workspace_worker.py')
        result = subprocess.run([sys.executable, str(worker), str(self.root), kind], capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 91, result.stderr.decode(errors='replace'))
        entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_crashed_multi_workspace_is_registered_and_cleaned_as_one_unit(self) -> None:
        entry = self.crash('multi')
        path = Path(str(entry['path']))
        self.assertEqual(entry['kind'], 'multi')
        self.assertTrue((path / 'vaults' / '00' / 'keys' / 'recovery.json').exists())
        self.assertTrue((path / 'vaults' / '15' / 'inbox' / 'inbox.sqlite3').exists())
        with self.assertRaises(MemoryFailure):
            cleanup_workspaces(self.config, Access('unauthorized', 'unauthorized'), [str(entry['id'])])
        result = cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(result['removed'], 1)
        self.assertFalse(result['physical_erasure_verified'])
        self.assertFalse(path.exists())

    def test_candidate_success_error_and_crash_remain_registered(self) -> None:
        runtime = MemoryRuntime(self.config, service_owner=True)
        with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            with candidate_workspace(runtime, self.root) as candidate:
                provision_private_directory(candidate / 'vaults')
            core.execute('COMMIT')
        entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'candidate')
        self.assertTrue(candidate.exists())
        old_python = os.environ.get('EVERNOOK_TEST_OLD_WORKSPACE_PYTHON')
        if old_python:
            code = ('from evernook import MemoryRuntime; from evernook.vault_workspace import list_workspaces; '
                    'from pathlib import Path; import sys; p=Path(sys.argv[1]); '
                    'a=MemoryRuntime(p, service_owner=True).access_from_file(p.parent / "mcp-access-owner.json"); '
                    'list_workspaces(p, a)')
            previous = subprocess.run([old_python, '-B', '-c', code, str(self.config)], cwd=str(self.root),
                                      env={**os.environ, 'PYTHONPATH': ''}, capture_output=True, timeout=30)
            self.assertNotEqual(previous.returncode, 0)
            self.assertIn(b'vault_workspace_registry_invalid', previous.stderr)
        cleanup_workspaces(self.config, self.owner, [entries[0]['id']])
        self.assertFalse(candidate.exists())
        with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError, 'synthetic'):
                with candidate_workspace(runtime, self.root) as failed:
                    provision_private_directory(failed / 'vaults')
                    raise ValueError('synthetic')
            core.execute('COMMIT')
        failed_entries = list_workspaces(self.config, self.owner)
        self.assertEqual(len(failed_entries), 1)
        self.assertTrue(failed.exists())
        cleanup_workspaces(self.config, self.owner, [failed_entries[0]['id']])
        entry = self.crash('candidate')
        self.assertTrue((Path(str(entry['path'])) / 'vaults' / '15' / 'inbox' / 'inbox.sqlite3').exists())
        self.assertEqual(cleanup_workspaces(self.config, self.owner, [str(entry['id'])])['removed'], 1)

    def test_multi_workspace_refuses_unknown_slot_or_file(self) -> None:
        entry = self.crash('multi')
        path = Path(str(entry['path']))
        unknown_slot = path / 'vaults' / '16'
        unknown_slot.mkdir()
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unknown_content'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        unknown_slot.rmdir()
        unknown_file = path / 'vaults' / '00' / 'keys' / 'other.key'
        unknown_file.write_bytes(b'keep')
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unknown_content'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(unknown_file.read_bytes(), b'keep')
        unknown_file.unlink()
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_multi_workspace_does_not_follow_slot_reparse_point(self) -> None:
        entry = self.crash('multi')
        path = Path(str(entry['path']))
        victim = self.root / 'multi-victim'
        victim.mkdir()
        (victim / 'keep').write_text('keep', encoding='utf-8')
        link = path / 'vaults' / '01'
        try:
            os.symlink(victim, link, target_is_directory=True)
        except OSError:
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(victim)],
                                    capture_output=True, timeout=15)
            if result.returncode:
                cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
                self.skipTest('Windows symlink and junction creation are unavailable')
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unsafe'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual((victim / 'keep').read_text(encoding='utf-8'), 'keep')
        link.rmdir()
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_real_crash_is_recorded_and_only_selected_workspace_is_cleaned(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        unrelated = self.root / '.evernook-vault-restore-unregistered'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('keep', encoding='utf-8')
        with self.assertRaises(MemoryFailure):
            list_workspaces(self.config, Access('unauthorized', 'unauthorized'))
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_not_registered'):
            cleanup_workspaces(self.config, self.owner, ['unregistered'])
        result = cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(result['removed'], 1)
        self.assertFalse(result['physical_erasure_verified'])
        self.assertFalse(path.exists())
        self.assertEqual((unrelated / 'keep').read_text(encoding='utf-8'), 'keep')
        self.assertEqual(list_workspaces(self.config, self.owner), [])

    def test_tampered_registry_fails_closed(self) -> None:
        entry = self.crash()
        registry = self.config.parent / 'vault-workspaces.json'
        original = registry.read_bytes()
        registry.write_bytes(original.replace(b'"authentication":"', b'"authentication":"0', 1))
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_registry_invalid'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertTrue(Path(str(entry['path'])).exists())
        registry.write_bytes(original)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_missing_registry_or_marker_fails_closed(self) -> None:
        entry = self.crash()
        for name in ('vault-workspaces.json', 'vault-workspaces-required.json'):
            path = self.config.parent / name
            saved = path.read_bytes()
            path.unlink()
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_registry_invalid'):
                list_workspaces(self.config, self.owner)
            self.assertTrue(Path(str(entry['path'])).exists())
            path.write_bytes(saved)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_unexpected_content_refuses_deletion_and_can_be_retried(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        unknown = path / 'do-not-delete'
        unknown.write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unknown_content'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(unknown.read_text(encoding='utf-8'), 'keep')
        unknown.unlink()
        self.assertEqual(cleanup_workspaces(self.config, self.owner, [str(entry['id'])])['removed'], 1)

    def test_interrupted_atomic_configuration_write_is_cleanable(self) -> None:
        entry = self.crash()
        ledger = Path(str(entry['path'])) / 'ledger'
        (ledger / '.pair-synthetic').write_text('synthetic temporary configuration', encoding='utf-8')
        (ledger / '.provision-synthetic').write_bytes(b'synthetic temporary key')
        self.assertEqual(cleanup_workspaces(self.config, self.owner, [str(entry['id'])])['removed'], 1)

    def test_replaced_directory_is_not_removed(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        moved = self.root / 'saved-original'
        path.rename(moved)
        path.mkdir()
        (path / 'keep').write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unsafe'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual((path / 'keep').read_text(encoding='utf-8'), 'keep')
        (path / 'keep').unlink()
        path.rmdir()
        moved.rename(path)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_missing_registered_path_is_not_forgotten_or_reported_destroyed(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        moved = self.root / 'moved-workspace'
        path.rename(moved)
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_missing'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(len(list_workspaces(self.config, self.owner)), 1)
        self.assertTrue((moved / 'ledger' / 'idempotency.key').exists())
        moved.rename(path)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_held_handles_block_workspace_and_child_replacement(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        original = verify_private_directory
        attempts: list[str] = []
        def race(root: Path, **kwargs: object) -> None:
            if root == path / 'ledger':
                for target in (path, root / 'idempotency.key'):
                    try:
                        target.rename(target.with_name(target.name + '-replacement'))
                    except OSError:
                        attempts.append(target.name)
                    else:
                        self.fail('opened cleanup target could be renamed')
            original(root, **kwargs)  # type: ignore[arg-type]
        with patch('evernook.vault_workspace.verify_private_directory', side_effect=race):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(len(attempts), 2)

    def test_reparse_point_is_never_followed(self) -> None:
        entry = self.crash()
        path = Path(str(entry['path']))
        victim = self.root / 'unrelated-victim'
        victim.mkdir()
        (victim / 'keep').write_text('keep', encoding='utf-8')
        link = path / 'vault'
        try:
            os.symlink(victim, link, target_is_directory=True)
        except OSError:
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(victim)],
                                    capture_output=True, timeout=15)
            if result.returncode:
                cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
                self.skipTest('Windows symlink and junction creation are unavailable')
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unsafe'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual((victim / 'keep').read_text(encoding='utf-8'), 'keep')
        link.rmdir()
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_deletion_followed_by_registry_failure_stays_unknown(self) -> None:
        entry = self.crash()
        with patch('evernook.vault_workspace._save', side_effect=MemoryFailure('synthetic-write-failure')):
            with self.assertRaisesRegex(MemoryFailure, 'synthetic-write-failure'):
                cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertFalse(Path(str(entry['path'])).exists())
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_missing'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(len(list_workspaces(self.config, self.owner)), 1)

    def test_filesystem_error_keeps_record_and_returns_a_stable_error(self) -> None:
        entry = self.crash()
        with patch('evernook.vault_workspace._remove_checked', side_effect=PermissionError('synthetic error')):
            with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_cleanup_failed'):
                cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertEqual(len(list_workspaces(self.config, self.owner)), 1)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_cleanup_refuses_running_service_and_noninteractive_cli(self) -> None:
        entry = self.crash()
        bound_config, binding = self.config.read_bytes(), self.binding_path.read_bytes()
        self.binding_path.unlink()
        self.config.write_bytes(self.base_config)
        self.process = subprocess.Popen([sys.executable, '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_file)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        endpoint, deadline = self.config.parent / 'memory-service.json', time.monotonic() + 30
        while not endpoint.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.fail('synthetic service failed to start')
            time.sleep(.05)
        with self.assertRaisesRegex(MemoryFailure, 'service_already_running'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.stop()
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'backup-vault-workspace-clean', '--config',
            str(self.config), '--access', str(self.owner_file), '--id', str(entry['id'])], input=b'', capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'interactive_recovery_required', result.stdout)
        self.assertTrue(Path(str(entry['path'])).exists())
        with self.assertRaisesRegex(MemoryFailure, 'vault_workspace_unbound_active'):
            cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.binding_path.write_bytes(binding)
        self.config.write_bytes(bound_config)
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])

    def test_acl_failure_never_deletes_workspace(self) -> None:
        entry = self.crash()
        original = verify_private_directory
        def reject_workspace(root: Path, **kwargs: object) -> None:
            if root == Path(str(entry['path'])):
                raise MemoryFailure('unsafe_runtime_permissions')
            original(root, **kwargs)  # type: ignore[arg-type]
        with patch('evernook.vault_workspace.verify_private_directory', side_effect=reject_workspace):
            with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
        self.assertTrue(Path(str(entry['path'])).exists())
        cleanup_workspaces(self.config, self.owner, [str(entry['id'])])
