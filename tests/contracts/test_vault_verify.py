from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.protected import initialize_protected_inbox
from evernook.vault_verify import verify_page


class VaultVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-verify-vault-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.runtime = MemoryRuntime(self.config)
        self.access_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.access_path)
        self.password = b'synthetic recovery password 203'
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys', self.password)
        self.event = Event(Source('owner', 'session', 'main', 'one'), 'user', 1, None, 'complete', 'synthetic-secret-canary-203')

    def test_linked_and_pending_are_decryptable_without_returning_plaintext(self) -> None:
        linked = self.inbox.submit(self.owner, self.event)
        pending = self.inbox.stage(self.owner, replace(self.event, source=Source('owner', 'session', 'main', 'two')))
        first = verify_page(self.inbox, self.owner, self.password, limit=1)
        self.assertIsNotNone(first['next_cursor'])
        second = verify_page(self.inbox, self.owner, self.password, after=first['next_cursor'], limit=1)
        self.assertIsNone(second['next_cursor'])
        entries = first['objects'] + second['objects']
        self.assertEqual({item['object_id'] for item in entries}, {linked.object_id, pending.object_id})
        self.assertEqual({item['state'] for item in entries}, {'protected_linked', 'protected_pending_link'})
        self.assertNotIn('synthetic-secret-canary-203', json.dumps([first, second]))
        self.assertNotIn(self.password.decode(), json.dumps([first, second]))
        for directory in (self.runtime.root, self.inbox.root, self.inbox.key_root):
            for path in directory.iterdir():
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(b'synthetic-secret-canary-203', data, str(path))
                    self.assertNotIn(self.password, data, str(path))

    def test_password_scope_and_noninteractive_cli_fail_closed(self) -> None:
        self.inbox.submit(self.owner, self.event)
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            verify_page(self.inbox, self.owner, b'incorrect password 123')
        ordinary = pair_client(self.config, client_id='ordinary', entry='ordinary',
            access_path=self.config.parent / 'mcp-access-ordinary.json', scopes=('capture', 'recall'))
        with patch('evernook.vault_verify._unlock', side_effect=AssertionError('unauthorized unlock')):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                verify_page(self.inbox, ordinary, self.password)
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'vault-verify', '--config', str(self.config),
            '--access', str(self.access_path), '--inbox-dir', str(self.root / 'inbox'), '--key-dir', str(self.root / 'keys')],
            input=self.password, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'interactive_vault_verification_required', result.stdout)
        self.assertNotIn(self.password, result.stdout + result.stderr)
        self.assertNotIn(b'synthetic-secret-canary-203', result.stdout + result.stderr)

    def test_trusted_interactive_cli_returns_only_page_metadata(self) -> None:
        from evernook.cli import _execute, _parser
        item = self.inbox.submit(self.owner, self.event)
        args = _parser().parse_args(['vault-verify', '--config', str(self.config), '--access', str(self.access_path),
            '--inbox-dir', str(self.root / 'inbox'), '--key-dir', str(self.root / 'keys'), '--limit', '1'])
        with patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=self.password.decode()) as prompt:
            result = cast(dict[str, Any], _execute(args))
        prompt.assert_called_once()
        self.assertEqual(result['objects'][0]['object_id'], item.object_id)
        self.assertNotIn('synthetic-secret-canary-203', json.dumps(result))
        self.assertNotIn(self.password.decode(), json.dumps(result))

    def test_invalid_page_and_cursor_are_rejected(self) -> None:
        for limit in (True, 0, 21):
            with self.subTest(limit=limit), self.assertRaisesRegex(MemoryFailure, 'invalid_page'):
                verify_page(self.inbox, self.owner, self.password, limit=limit)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_id'):
            verify_page(self.inbox, self.owner, self.password, after='invalid')

    def test_verification_module_does_not_require_tk(self) -> None:
        result = subprocess.run([sys.executable, '-B', '-c',
            "import sys; sys.modules['tkinter'] = None; import evernook.vault_verify"],
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_ciphertext_or_missing_recovery_material_never_passes(self) -> None:
        item = self.inbox.submit(self.owner, self.event)
        with self.inbox._connection() as db:
            db.execute("UPDATE objects SET envelope='{}' WHERE id=?", (item.object_id,))
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            verify_page(self.inbox, self.owner, self.password)
        (self.root / 'keys' / 'recovery.json').unlink()
        with self.assertRaises(MemoryFailure):
            verify_page(self.inbox, self.owner, self.password)

    def test_validly_signed_pending_envelope_with_bad_ciphertext_fails_decryption(self) -> None:
        from evernook.runtime import _json
        pending = self.inbox.stage(self.owner, self.event)
        with self.inbox._connection() as db:
            row = db.execute('SELECT envelope FROM objects WHERE id=?', (pending.object_id,)).fetchone()
            value = json.loads(row['envelope'])
            cipher = bytearray.fromhex(value['ciphertext'])
            cipher[-1] ^= 1
            value['ciphertext'] = cipher.hex()
            value['signature'] = self.inbox._writer().sign(_json({'bound': value['bound'], 'ciphertext': value['ciphertext']}).encode()).signature.hex()
            db.execute('UPDATE objects SET envelope=? WHERE id=?', (_json(value), pending.object_id))
        self.assertEqual(len(self.inbox.inventory(self.owner).objects), 1)
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            verify_page(self.inbox, self.owner, self.password)

    def test_signed_decryptable_event_with_inconsistent_projection_is_rejected(self) -> None:
        from nacl.public import SealedBox
        from evernook.runtime import _json
        pending = self.inbox.stage(self.owner, self.event)
        with self.inbox._connection() as db:
            row = db.execute('SELECT envelope FROM objects WHERE id=?', (pending.object_id,)).fetchone()
            value = json.loads(row['envelope'])
            value['bound']['projection']['role'] = 'assistant'
            value['ciphertext'] = SealedBox(self.inbox.recipient).encrypt(
                _json({'bound': value['bound'], 'event': asdict(self.event)}).encode()).hex()
            value['signature'] = self.inbox._writer().sign(_json({'bound': value['bound'], 'ciphertext': value['ciphertext']}).encode()).signature.hex()
            db.execute('UPDATE objects SET envelope=? WHERE id=?', (_json(value), pending.object_id))
        self.assertEqual(len(self.inbox.inventory(self.owner).objects), 1)
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            verify_page(self.inbox, self.owner, self.password)

    def test_concurrent_reconcile_does_not_turn_page_snapshot_into_migration_proof(self) -> None:
        from evernook.vault_crypto import _decrypt_event
        pending = self.inbox.stage(self.owner, self.event)
        def reconcile_then_verify(*args: Any, **kwargs: Any) -> object:
            self.inbox.reconcile(self.owner, pending.object_id)
            return _decrypt_event(*args, **kwargs)
        with patch('evernook.vault_verify._decrypt_event', side_effect=reconcile_then_verify):
            page = verify_page(self.inbox, self.owner, self.password)
        self.assertEqual(page['objects'][0]['state'], 'protected_pending_link')
        self.assertFalse(page['migration_authorized'])
        self.assertEqual(self.inbox.status(self.owner, pending.object_id).state, 'protected_linked')

    def test_permission_change_before_return_discards_verification(self) -> None:
        self.inbox.submit(self.owner, self.event)
        original = self.runtime._require_scope
        calls = 0
        def revoke(*args: Any, **kwargs: Any) -> object:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise MemoryFailure('permission_denied')
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_require_scope', side_effect=revoke):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                verify_page(self.inbox, self.owner, self.password)


if __name__ == '__main__':
    unittest.main()
