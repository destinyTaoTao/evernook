from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, cast
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, Source, pair_client
from evernook.runtime import _atomic_json
from evernook.vault_multi_proof import PROOF_NAME, _authentication, prepare, revalidate
from tests.contracts.test_vault_multi_verify import MultiVaultFixture


class MultiVaultProofTests(MultiVaultFixture):
    def complete(self) -> None:
        self.second.reconcile(self.owner, self.pending.object_id)
        self.second.reconcile(self.owner, self.ack.object_id)

    def prepare(self) -> dict[str, object]:
        return prepare(self.runtime, self.owner, self.declared, inventory_statement='STOPPED_AND_INVENTORIED')

    def check(self) -> dict[str, object]:
        return revalidate(self.runtime, self.owner, (self.first, self.second))

    def test_proof_round_trip_and_stale_visibility(self) -> None:
        self.complete()
        result = self.prepare()
        self.assertTrue(result['proof_saved'])
        self.assertFalse(result['migration_completed'])
        self.assertTrue(self.check()['proof_valid'])
        self.assertTrue(revalidate(self.runtime, self.owner, (self.second, self.first))['proof_valid'])
        raw = (self.runtime.root / PROOF_NAME).read_bytes()
        self.assertNotIn(self.passwords[0], raw)
        self.assertNotIn(self.event.text.encode(), raw)
        control = self.runtime.submit(self.owner, Event(Source('owner', 'synthetic', 'main', 'control'),
                                      'user', 2, None, 'complete', 'synthetic suppression instruction'))
        assert self.linked.message is not None
        self.runtime.change(self.owner, Change('synthetic-suppress-231', self.linked.message.message_id, 1,
                                              'suppress', control.message_id, 'synthetic'))
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_stale'):
            self.check()

    def test_client_and_object_changes_invalidate_snapshot(self) -> None:
        self.complete()
        self.prepare()
        pair_client(self.config, client_id='synthetic-new-reader', entry='new-reader',
                    access_path=self.config.parent / 'mcp-access-new-reader.json', scopes=('recall',))
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_stale'):
            self.check()
        self.prepare()
        self.first.submit(self.owner, replace(self.event, source=replace(self.event.source, event='new-linked')))
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_stale'):
            self.check()
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_inbox_set'):
            revalidate(self.runtime, self.owner, (self.first, self.first))
        self.prepare()
        with self.first._connection() as db:
            row = db.execute('SELECT envelope FROM objects WHERE id=?', (self.linked.object_id,)).fetchone()
            envelope = json.loads(row['envelope'])
            first = envelope['ciphertext'][0]
            envelope['ciphertext'] = ('0' if first != '0' else '1') + envelope['ciphertext'][1:]
            db.execute('UPDATE objects SET envelope=? WHERE id=?', (json.dumps(envelope), self.linked.object_id))
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            self.check()

    def test_pending_and_bad_password_invalidate_old_proof(self) -> None:
        self.complete()
        self.prepare()
        wrong = ((self.first, self.passwords[0]), (self.second, b'synthetic wrong password'))
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            prepare(self.runtime, self.owner, wrong, inventory_statement='STOPPED_AND_INVENTORIED')
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        self.prepare()
        self.second.stage(self.owner, replace(self.event, source=replace(self.event.source, event='new-pending')))
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            self.prepare()
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_tamper_expiry_and_key_change_are_refused(self) -> None:
        self.complete()
        self.prepare()
        path = self.runtime.root / PROOF_NAME
        proof = json.loads(path.read_text(encoding='utf-8'))
        proof['snapshot']['objects'] = 0
        _atomic_json(path, proof)
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_invalid'):
            self.check()
        self.prepare()
        proof = json.loads(path.read_text(encoding='utf-8'))
        proof.pop('authentication')
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        proof['issued_at'] = old.isoformat()
        proof['expires_at'] = (old + timedelta(minutes=10)).isoformat()
        proof['authentication'] = _authentication(self.runtime, proof)
        _atomic_json(path, proof)
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_invalid'):
            self.check()
        self.prepare()
        recovery = self.second.key_root / 'recovery.json'
        recovery.write_bytes(recovery.read_bytes() + b'\n')
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_stale'):
            self.check()

    def test_replaced_ledger_key_is_rejected_even_in_existing_process(self) -> None:
        self.complete()
        self.prepare()
        path = self.runtime.root / 'idempotency.key'
        original = path.read_bytes()
        try:
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            with self.assertRaisesRegex(MemoryFailure, 'protected_migration_material_changed'):
                self.check()
        finally:
            path.write_bytes(original)
        with patch('evernook.vault_multi_verify.verify_private_directory', side_effect=MemoryFailure('unsafe_permissions')):
            with self.assertRaisesRegex(MemoryFailure, 'unsafe_permissions'):
                self.check()

    def test_crash_after_publication_keeps_revalidatable_proof(self) -> None:
        self.complete()
        worker = Path(__file__).with_name('vault_multi_proof_crash_worker.py')
        process = subprocess.run([sys.executable, '-B', str(worker), str(self.config), str(self.owner_file), str(self.root)],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=40)
        self.assertEqual(process.returncode, 91, process.stderr)
        self.assertTrue(self.check()['proof_valid'])

    def test_preparation_cli_requires_real_console(self) -> None:
        command = [sys.executable, '-m', 'evernook.cli', 'vault-prepare-set', '--config', str(self.config),
                   '--access', str(self.owner_file), '--inbox-dir', str(self.first.root), '--key-dir', str(self.first.key_root),
                   '--inbox-dir', str(self.second.root), '--key-dir', str(self.second.key_root)]
        for options in ({'input': b''}, {'stdin': subprocess.DEVNULL}):
            result = subprocess.run(command, **options, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'interactive_vault_verification_required')
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_interactive_cancel_invalidates_previous_proof_before_password_prompt(self) -> None:
        from evernook.cli import _execute, _parser
        self.complete()
        self.prepare()
        args = _parser().parse_args(['vault-prepare-set', '--config', str(self.config), '--access', str(self.owner_file),
            '--inbox-dir', str(self.first.root), '--key-dir', str(self.first.key_root),
            '--inbox-dir', str(self.second.root), '--key-dir', str(self.second.key_root)])
        with patch('evernook.cli._trusted_console', return_value=True), patch('builtins.input', return_value='no'), \
                patch('getpass.getpass') as prompt:
            with self.assertRaisesRegex(MemoryFailure, 'protected_migration_inventory_unconfirmed'):
                _execute(args)
            prompt.assert_not_called()
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        with patch('evernook.cli._trusted_console', return_value=True), \
                patch('builtins.input', return_value='STOPPED_AND_INVENTORIED'), \
                patch('getpass.getpass', side_effect=[x.decode() for x in self.passwords]):
            result = cast(dict[str, Any], _execute(args))
        self.assertTrue(result['proof_saved'])
        self.assertNotIn(self.passwords[0].decode(), str(result))
        self.assertTrue(self.check()['proof_valid'])
        command = [sys.executable, '-m', 'evernook.cli', 'vault-check-set', '--config', str(self.config),
                   '--access', str(self.owner_file), '--inbox-dir', str(self.first.root), '--key-dir', str(self.first.key_root),
                   '--inbox-dir', str(self.second.root), '--key-dir', str(self.second.key_root)]
        checked = subprocess.run(command, input=b'', capture_output=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertTrue(json.loads(checked.stdout)['result']['proof_valid'])
        self.assertNotIn(self.event.text.encode(), checked.stdout + checked.stderr)


if __name__ == '__main__':
    unittest.main()
