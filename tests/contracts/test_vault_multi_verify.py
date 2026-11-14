from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.protected import ProtectedReceipt, initialize_protected_inbox
from evernook.vault_multi_verify import verify_declared


class MultiVaultFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-multi-verify-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner',
                                             allow_archive_review=getattr(self, 'archive_review', False))
        self.runtime = MemoryRuntime(self.config)
        self.owner_file = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_file)
        self.reader = pair_client(self.config, client_id='reader', entry='reader',
                                  access_path=self.config.parent / 'mcp-access-reader.json', scopes=('recall',))
        self.passwords = (b'synthetic multi vault password one', b'synthetic multi vault password two')
        self.first = initialize_protected_inbox(self.runtime, self.root / 'inbox-a', self.root / 'keys-a', self.passwords[0])
        self.second = initialize_protected_inbox(self.runtime, self.root / 'inbox-b', self.root / 'keys-b', self.passwords[1])
        self.event = Event(Source('owner', 'synthetic', 'main', 'one'), 'user', 1, None, 'complete',
                           'synthetic-multi-secret-229', sensitive=True)
        self.linked = self.first.submit(self.owner, self.event)
        self.pending = self.second.stage(self.owner, replace(self.event, source=replace(self.event.source, event='two')))
        self.ack = self.second.submit(self.owner, replace(self.event, source=replace(self.event.source, event='three')),
                                      stream_provenance=True)
        with self.second._connection() as db:
            db.execute("UPDATE objects SET state='protected_pending_link',receipt=NULL WHERE id=?", (self.ack.object_id,))
        self.declared = ((self.first, self.passwords[0]), (self.second, self.passwords[1]))


class MultiVaultVerifyTests(MultiVaultFixture):
    def test_all_inboxes_decrypt_and_both_pending_windows_are_reported(self) -> None:
        with self.first._connection() as db:
            before_first = [tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')]
        with self.second._connection() as db:
            before_second = [tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')]
        result = verify_declared(self.runtime, self.owner, self.declared)
        self.assertEqual(result['objects'], 3)
        self.assertEqual(result['linked_references'], 2)
        self.assertEqual(result['pending'], 2)
        self.assertEqual(result['pending_with_link'], 1)
        self.assertFalse(result['migration_authorized'])
        self.assertFalse(result['unknown_inboxes_discoverable'])
        self.assertNotIn(self.event.text, json.dumps(result))
        self.assertNotIn(self.passwords[0].decode(), json.dumps(result))
        with self.first._connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')], before_first)
        with self.second._connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')], before_second)
        self.assertEqual(self.second.reconcile(self.owner, self.ack.object_id).object_id, self.ack.object_id)
        self.assertEqual(self.second.reconcile(self.owner, self.pending.object_id).object_id, self.pending.object_id)
        self.assertEqual(verify_declared(self.runtime, self.owner, self.declared)['pending'], 0)

    def test_wrong_password_omissions_duplicates_and_unprivileged_access_fail(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            verify_declared(self.runtime, self.owner, ((self.first, self.passwords[0]), (self.second, b'synthetic-wrong-password')))
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_inbox_set'):
            verify_declared(self.runtime, self.owner, ((self.first, self.passwords[0]), (self.first, self.passwords[0])))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            verify_declared(self.runtime, self.reader, self.declared)
        third = initialize_protected_inbox(self.runtime, self.root / 'inbox-c', self.root / 'keys-c', b'synthetic third password')
        third.register(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            verify_declared(self.runtime, self.owner, self.declared)

    def test_nested_inbox_directories_are_rejected(self) -> None:
        third_password = b'synthetic third password'
        third = initialize_protected_inbox(self.runtime, self.first.root / 'nested', self.root / 'keys-c', third_password)
        third.register(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_inbox_set'):
            verify_declared(self.runtime, self.owner, (*self.declared, (third, third_password)))

    def test_tampered_object_and_missing_key_fail_closed(self) -> None:
        with self.second._connection() as db:
            original = db.execute('SELECT envelope FROM objects WHERE id=?', (self.pending.object_id,)).fetchone()[0]
            db.execute("UPDATE objects SET envelope='{}' WHERE id=?", (self.pending.object_id,))
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            verify_declared(self.runtime, self.owner, self.declared)
        with self.second._connection() as db:
            db.execute('UPDATE objects SET envelope=? WHERE id=?', (original, self.pending.object_id))
        (self.root / 'keys-b' / 'recovery.json').unlink()
        with self.assertRaises(MemoryFailure):
            verify_declared(self.runtime, self.owner, self.declared)

    def test_cross_inbox_duplicate_source_and_object_limit_fail(self) -> None:
        with patch('evernook.vault_multi_verify.MAX_OBJECTS', 2):
            with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
                verify_declared(self.runtime, self.owner, self.declared)
        self.second.stage(self.owner, self.event)
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            verify_declared(self.runtime, self.owner, self.declared)

    def test_changed_reverse_reference_fails_closed(self) -> None:
        assert self.linked.message is not None
        with self.runtime._connection() as db:
            db.execute("UPDATE protected_links_v1 SET reference='{}' WHERE message_id=?", (self.linked.message.message_id,))
        with self.assertRaisesRegex(MemoryFailure, 'protected_link_unavailable'):
            verify_declared(self.runtime, self.owner, self.declared)

    def test_exclusive_preflight_blocks_cooperative_old_writer(self) -> None:
        import evernook.vault_multi_verify as module
        entered, release, writer_entered = threading.Event(), threading.Event(), threading.Event()
        from evernook.vault_crypto import _decrypt_event
        original = _decrypt_event
        def paused(*args: object, **kwargs: object) -> object:
            entered.set()
            if not release.wait(20):
                raise RuntimeError('synthetic verifier timeout')
            return original(*args, **kwargs)  # type: ignore[arg-type]
        def write() -> ProtectedReceipt:
            writer_entered.set()
            return self.first.stage(self.owner, replace(self.event, source=replace(self.event.source, event='later')))
        with patch.object(module, '_decrypt_event', side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
            verification = pool.submit(verify_declared, self.runtime, self.owner, self.declared)
            try:
                self.assertTrue(entered.wait(20))
                writer = pool.submit(write)
                self.assertTrue(writer_entered.wait(5))
                with self.assertRaises(TimeoutError):
                    writer.result(timeout=0.2)
            finally:
                release.set()
            self.assertEqual(verification.result(timeout=30)['objects'], 3)
            self.assertEqual(writer.result(timeout=30).state, 'protected_pending_link')

    def test_cli_is_interactive_and_never_outputs_password_or_content(self) -> None:
        from evernook.cli import _execute, _parser
        command = ['vault-verify-set', '--config', str(self.config), '--access', str(self.owner_file),
                   '--inbox-dir', str(self.first.root), '--key-dir', str(self.first.key_root),
                   '--inbox-dir', str(self.second.root), '--key-dir', str(self.second.key_root)]
        for options in ({'input': b''}, {'stdin': subprocess.DEVNULL}):
            result = subprocess.run([sys.executable, '-m', 'evernook.cli', *command],
                                    **options, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'interactive_vault_verification_required', result.stdout)
        with patch('evernook.cli._trusted_console', return_value=True), patch('getpass.getpass', side_effect=[x.decode() for x in self.passwords]):
            result_value = _execute(_parser().parse_args(command))
        self.assertNotIn(self.event.text, str(result_value))
        self.assertNotIn(self.passwords[0].decode(), str(result_value))


if __name__ == '__main__':
    unittest.main()
