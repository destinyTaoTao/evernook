from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, cast
import unittest
from unittest.mock import patch

from evernook import Access, Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.protected import ProtectedInbox, initialize_protected_inbox
from evernook.service import ServiceClient
from evernook.vault_migration import PROOF_NAME, prepare as prepare_impl


def prepare(runtime: MemoryRuntime, inbox: ProtectedInbox, access: Access, password: bytes) -> dict[str, object]:
    return prepare_impl(runtime, inbox, access, password, inventory_statement='STOPPED_AND_INVENTORIED')


class VaultMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-vault-migration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path)
        self.password = b'synthetic migration password 204'
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys', self.password)
        self.event = Event(Source('owner', 'private', 'main', 'one'), 'user', 1, None,
            'complete', 'synthetic-migration-secret-204', sensitive=True)
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)

    def command(self, *, verified: bool = True) -> list[str]:
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'keys')]
        return args + (['--activate-verified-inbox'] if verified else [])

    def start(self, args: list[str]) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        previous = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == previous:
            if process.poll() is not None:
                assert process.stderr is not None
                self.fail('service exited: ' + process.stderr.read().decode(errors='replace'))
            if time.monotonic() >= deadline:
                self.fail('service start timeout')
            time.sleep(.05)
        return ServiceClient(self.config)

    def test_inventory_and_reconcile_multiple_entries_before_verified_cutover(self) -> None:
        from evernook.vault_migration import inventory_entries
        from evernook.vault_verify import verify_page
        from evernook.vault_local import LocalDisplayRequest
        other_path = self.config.parent / 'mcp-access-other.json'
        other = pair_client(self.config, client_id='other', entry='other', access_path=other_path,
                            scopes=('capture', 'recall'))
        linked = self.inbox.submit(self.owner, self.event)
        other_event = replace(self.event, source=Source('other', 'private', 'main', 'pending'),
                              text='synthetic-other-pending-226')
        pending = self.inbox.stage(other, other_event)
        first = inventory_entries(self.inbox, self.owner, limit=1)
        self.assertIsNotNone(first['next_cursor'])
        second = inventory_entries(self.inbox, self.owner, after=str(first['next_cursor']), limit=1)
        items = cast(list[dict[str, str]], first['objects']) + cast(list[dict[str, str]], second['objects'])
        self.assertEqual({(x['object_id'], x['entry'], x['state']) for x in items},
                         {(linked.object_id, 'owner', 'protected_linked'),
                          (pending.object_id, 'other', 'protected_pending_link')})
        self.assertNotIn('synthetic-other-pending-226', json.dumps(items))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            inventory_entries(self.inbox, other)
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            self.inbox.reconcile(self.owner, pending.object_id)
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            verify_page(self.inbox, self.owner, b'synthetic wrong password')
        cli = subprocess.run([sys.executable, '-m', 'evernook.cli', 'vault-migration-inventory', '--config', str(self.config),
            '--access', str(self.owner_path), '--inbox-dir', str(self.root / 'inbox'), '--key-dir', str(self.root / 'keys')],
            capture_output=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(len(json.loads(cli.stdout)['result']['objects']), 2)
        self.assertNotIn(other_event.text.encode(), cli.stdout + cli.stderr)
        restarted = ProtectedInbox(MemoryRuntime(self.config), self.root / 'inbox', self.root / 'keys')
        self.assertEqual(restarted.reconcile(other, pending.object_id).state, 'protected_linked')
        self.assertEqual(restarted.reconcile(other, pending.object_id).object_id, pending.object_id)
        self.assertEqual(len(verify_page(restarted, self.owner, self.password)['objects']), 2)
        self.assertEqual(prepare(restarted.runtime, restarted, self.owner, self.password)['verified_objects'], 2)
        client = self.start(self.command())
        self.assertTrue(client.call(self.owner, 'status', {})['service']['single_writer'])
        with self.assertRaisesRegex(MemoryFailure, 'service_required'):
            self.runtime.submit(self.owner, replace(self.event, source=Source('owner', 'old', 'main', 'blocked')))
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        installed = ProtectedInbox(MemoryRuntime(self.config, service_owner=True), self.root / 'inbox', self.root / 'keys')
        for access, receipt, text in ((self.owner, linked, self.event.text), (other, pending, other_event.text)):
            displayed: list[str] = []
            LocalDisplayRequest(installed, access, receipt.object_id, 'synthetic', desktop=lambda: True).display_once(self.password, displayed.append)
            self.assertEqual(displayed, [text])
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            LocalDisplayRequest(installed, self.owner, pending.object_id, 'synthetic', desktop=lambda: True)

    def test_migration_inventory_rejects_invalid_pages_tampering_and_revoked_admin(self) -> None:
        from evernook.vault_migration import inventory_entries
        staged = self.inbox.stage(self.owner, self.event)
        for limit in (0, 101, True):
            with self.subTest(limit=limit), self.assertRaisesRegex(MemoryFailure, 'invalid_page'):
                inventory_entries(self.inbox, self.owner, limit=limit)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_id'):
            inventory_entries(self.inbox, self.owner, after='not-an-id')
        original = self.runtime._require_scope
        calls = 0
        def revoked(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MemoryFailure('permission_denied')
            return original(*args, **kwargs)
        with patch.object(self.runtime, '_require_scope', side_effect=revoked):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                inventory_entries(self.inbox, self.owner)
        with self.inbox._connection() as db:
            db.execute("UPDATE objects SET envelope='{}' WHERE id=?", (staged.object_id,))
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            inventory_entries(self.inbox, self.owner)

    def test_complete_single_inbox_cutover_preserves_decryptability_and_old_writer_fence(self) -> None:
        original = self.inbox.submit(self.owner, self.event)
        stream_event = replace(self.event, source=Source('owner', 'private', 'main', 'stream'))
        stream = self.inbox.submit(self.owner, stream_event, stream_provenance=True)
        proof = prepare(self.runtime, self.inbox, self.owner, self.password)
        self.assertEqual(proof['verified_objects'], 2)
        self.assertFalse(proof['migration_completed'])
        raw = (self.runtime.root / PROOF_NAME).read_bytes()
        self.assertNotIn('link_digest', json.loads(raw)['snapshot'])
        self.assertNotIn(self.password, raw)
        self.assertNotIn(b'synthetic-migration-secret-204', raw)
        old = subprocess.run(self.command(verified=False), capture_output=True, timeout=30)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', old.stderr)
        client = self.start(self.command())
        self.assertTrue(client.call(self.owner, 'status', {})['service']['single_writer'])
        with self.assertRaisesRegex(MemoryFailure, 'operation_not_allowed'):
            client.call(self.owner, 'cli', {'command': 'vault-prepare-migration', 'input': {}})
        tools = client.call(self.owner, 'mcp-list', {'profile': 'trusted-host'})['tools']
        self.assertFalse(any('migration' in item['name'] for item in tools))
        with self.assertRaisesRegex(MemoryFailure, 'service_required'):
            self.runtime.submit(self.owner, replace(self.event, source=Source('owner', 'ordinary', 'main', 'old')))
        added = client.protected_receive(self.owner, replace(self.event, source=Source('owner', 'private', 'main', 'two')))
        self.assertEqual(added['state'], 'protected_linked')
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        self.start([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                    '--protected-inbox', str(self.root / 'inbox'), '--protected-keys', str(self.root / 'keys')])
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        from evernook.vault_local import LocalDisplayRequest
        service_runtime = MemoryRuntime(self.config, service_owner=True)
        restored = ProtectedInbox(service_runtime, self.root / 'inbox', self.root / 'keys')
        for object_id in (original.object_id, stream.object_id, added['object_id']):
            displayed: list[str] = []
            LocalDisplayRequest(restored, self.owner, object_id, 'synthetic', desktop=lambda: True).display_once(self.password, displayed.append)
            self.assertEqual(displayed, [self.event.text])

    def test_pending_wrong_password_and_untrusted_cli_never_prepare(self) -> None:
        pending = self.inbox.stage(self.owner, self.event)
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            prepare(self.runtime, self.inbox, self.owner, b'wrong password for synthetic')
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            prepare(self.runtime, self.inbox, self.owner, self.password)
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'vault-prepare-migration',
            '--config', str(self.config), '--access', str(self.owner_path), '--inbox-dir', str(self.root / 'inbox'),
            '--key-dir', str(self.root / 'keys')], input=self.password, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'interactive_vault_migration_required', result.stdout)
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        self.inbox.reconcile(self.owner, pending.object_id)
        self.assertEqual(prepare(self.runtime, self.inbox, self.owner, self.password)['verified_objects'], 1)

    def test_interactive_cli_requires_inventory_acknowledgement(self) -> None:
        from evernook.cli import _execute, _parser
        self.inbox.submit(self.owner, self.event)
        args = _parser().parse_args(['vault-prepare-migration', '--config', str(self.config),
            '--access', str(self.owner_path), '--inbox-dir', str(self.root / 'inbox'), '--key-dir', str(self.root / 'keys')])
        prepare(self.runtime, self.inbox, self.owner, self.password)
        with patch('evernook.cli._trusted_console', return_value=True), patch('builtins.input', return_value='no'), \
                patch('getpass.getpass') as password_prompt:
            with self.assertRaisesRegex(MemoryFailure, 'protected_migration_inventory_unconfirmed'):
                _execute(args)
            password_prompt.assert_not_called()
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        with patch('evernook.cli._trusted_console', return_value=True), patch('builtins.input', return_value='STOPPED_AND_INVENTORIED'), \
                patch('getpass.getpass', return_value=self.password.decode()):
            result = _execute(args)
        self.assertNotIn(self.password.decode(), str(result))
        self.assertNotIn('synthetic-migration-secret-204', str(result))

    def test_changed_objects_reject_old_proof_without_switching(self) -> None:
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        self.inbox.submit(self.owner, replace(self.event, source=Source('owner', 'private', 'main', 'two')))
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_migration_proof_stale', refused.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertFalse((self.config.parent / 'memory-service.json').exists())

    def test_proof_tampering_and_additional_registered_inbox_are_rejected(self) -> None:
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        proof_path = self.runtime.root / PROOF_NAME
        value = json.loads(proof_path.read_text(encoding='utf-8'))
        value['snapshot']['objects'] = 0
        proof_path.write_text(json.dumps(value), encoding='utf-8')
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_migration_proof_invalid', refused.stderr)
        second = initialize_protected_inbox(self.runtime, self.root / 'other-inbox', self.root / 'other-keys', self.password)
        second.register(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            prepare(self.runtime, self.inbox, self.owner, self.password)

    def test_changed_control_and_recovery_material_invalidate_proof(self) -> None:
        linked = self.inbox.submit(self.owner, self.event)
        assert linked.message is not None
        confirmation = self.runtime.submit(self.owner, Event(Source('owner', 'controls', 'main', 'one'),
            'user', 2, None, 'complete', 'synthetic conceal instruction')).message_id
        prepare(self.runtime, self.inbox, self.owner, self.password)
        self.runtime.change(self.owner, Change('synthetic-hide', linked.message.message_id, 1,
            'suppress', confirmation, 'synthetic control'))
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_migration_proof_stale', refused.stderr)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        recovery = self.root / 'keys' / 'recovery.json'
        recovery.write_text(recovery.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_migration_proof_stale', refused.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_recovery_material_changed_during_unlock_cannot_be_attested(self) -> None:
        from evernook.vault_crypto import _unlock
        self.inbox.submit(self.owner, self.event)
        def unlock_then_change(*args: Any, **kwargs: Any) -> Any:
            private = _unlock(*args, **kwargs)
            recovery = self.root / 'keys' / 'recovery.json'
            recovery.write_text(recovery.read_text(encoding='utf-8') + '\n', encoding='utf-8')
            return private
        with patch('evernook.vault_migration._unlock', side_effect=unlock_then_change):
            with self.assertRaisesRegex(MemoryFailure, 'protected_migration_material_changed'):
                prepare(self.runtime, self.inbox, self.owner, self.password)
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_failed_repreparation_removes_previous_proof(self) -> None:
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        self.assertTrue((self.runtime.root / PROOF_NAME).exists())
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            prepare(self.runtime, self.inbox, self.owner, b'incorrect synthetic password')
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        prepare(self.runtime, self.inbox, self.owner, self.password)
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_inventory_unconfirmed'):
            prepare_impl(self.runtime, self.inbox, self.owner, self.password, inventory_statement='not-inventoried')
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_orphan_projection_blocks_preparation(self) -> None:
        linked = self.inbox.submit(self.owner, self.event)
        assert linked.message is not None
        with closing(sqlite3.connect(self.runtime.root / 'memory.sqlite3', isolation_level=None)) as db:
            db.execute('DELETE FROM protected_links_v1 WHERE message_id=?', (linked.message.message_id,))
        with self.assertRaisesRegex(MemoryFailure, 'protected_link_unavailable'):
            prepare(self.runtime, self.inbox, self.owner, self.password)
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_preparation_deadline_does_not_publish_proof_or_change_assets(self) -> None:
        linked = self.inbox.submit(self.owner, self.event)
        with patch('evernook.vault_migration.MAX_SECONDS', 0.001):
            with self.assertRaisesRegex(MemoryFailure, 'protected_migration_timeout'):
                prepare(self.runtime, self.inbox, self.owner, self.password)
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())
        self.assertEqual(self.inbox.status(self.owner, linked.object_id).state, 'protected_linked')

    def test_entry_without_recall_identity_blocks_preparation(self) -> None:
        other_path = self.config.parent / 'mcp-access-other.json'
        other = pair_client(self.config, client_id='other', entry='other', access_path=other_path, scopes=('capture',))
        self.inbox.submit(other, replace(self.event, source=Source('other', 'private', 'main', 'one')))
        with self.assertRaisesRegex(MemoryFailure, 'protected_migration_unreachable_entry'):
            prepare(self.runtime, self.inbox, self.owner, self.password)
        pair_client(self.config, client_id='other', entry='other', access_path=other_path, scopes=('capture', 'recall'))
        self.assertEqual(prepare(self.runtime, self.inbox, self.owner, self.password)['verified_objects'], 1)

    def test_admin_revoked_after_initial_check_cannot_prepare_proof(self) -> None:
        self.inbox.submit(self.owner, self.event)
        ready, release = threading.Event(), threading.Event()
        original = self.runtime._require_scope
        def paused(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            ready.set()
            if not release.wait(20):
                raise RuntimeError('synthetic authorization barrier timed out')
            return result
        with patch.object(self.runtime, '_require_scope', side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(prepare, self.runtime, self.inbox, self.owner, self.password)
            try:
                self.assertTrue(ready.wait(10))
                pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path, scopes=('capture', 'recall'))
            finally:
                release.set()
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                future.result(timeout=25)
        self.assertFalse((self.runtime.root / PROOF_NAME).exists())

    def test_expired_proof_and_revoked_admin_are_rejected(self) -> None:
        from evernook.vault_migration import _authentication
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        proof_path = self.runtime.root / PROOF_NAME
        value = json.loads(proof_path.read_text(encoding='utf-8'))
        value.pop('authentication')
        value['issued_at'] = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
        value['expires_at'] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        value['authentication'] = _authentication(self.runtime, value)
        proof_path.write_text(json.dumps(value), encoding='utf-8')
        expired = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(expired.returncode, 0)
        self.assertIn(b'protected_migration_proof_invalid', expired.stderr)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path, scopes=('capture', 'recall'))
        denied = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(b'permission_denied', denied.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_proof_expiring_during_rescan_is_rejected(self) -> None:
        from evernook.vault_migration import verify_for_activation
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        now = datetime.now(timezone.utc)
        with self.runtime._connection(ownership_exclusive=True) as core, self.inbox._connection(gate_db=core) as store:
            core.execute('BEGIN IMMEDIATE')
            store.execute('BEGIN IMMEDIATE')
            with patch('evernook.vault_migration.datetime') as clock:
                clock.fromisoformat = datetime.fromisoformat
                clock.now.side_effect = [now, now + timedelta(minutes=11)]
                with self.assertRaisesRegex(MemoryFailure, 'protected_migration_proof_invalid'):
                    verify_for_activation(self.runtime, self.inbox, self.owner, core, store)

    def test_preparation_excludes_concurrent_old_writer_and_rejects_later_pending(self) -> None:
        from evernook.vault_crypto import _decrypt_event
        self.inbox.submit(self.owner, self.event)
        started, release = threading.Event(), threading.Event()
        def paused(*args: Any, **kwargs: Any) -> object:
            started.set()
            if not release.wait(20):
                raise RuntimeError('synthetic migration barrier timed out')
            return _decrypt_event(*args, **kwargs)
        with patch('evernook.vault_migration._decrypt_event', side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
            preparation = pool.submit(prepare, self.runtime, self.inbox, self.owner, self.password)
            self.assertTrue(started.wait(15))
            with closing(sqlite3.connect(self.inbox.root / 'inbox.sqlite3', timeout=.1, isolation_level=None)) as raw:
                with self.assertRaises(sqlite3.OperationalError):
                    raw.execute('BEGIN IMMEDIATE')
            writer = pool.submit(self.inbox.stage, self.owner,
                replace(self.event, source=Source('owner', 'private', 'main', 'later')))
            try:
                time.sleep(.2)
                self.assertFalse(writer.done())
            finally:
                release.set()
            self.assertEqual(preparation.result(timeout=25)['verified_objects'], 1)
            self.assertEqual(writer.result(timeout=25).state, 'protected_pending_link')
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', refused.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_activation_holds_inbox_write_lock_until_mode_is_persisted(self) -> None:
        self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        marker, release = self.root / 'activation-ready', self.root / 'activation-release'
        script = '''import sys, time
from pathlib import Path
import evernook.service as service
import evernook.vault_migration as migration
original = migration.verify_for_activation
def paused(*args):
    original(*args)
    Path(sys.argv[5]).write_text('ready')
    while not Path(sys.argv[6]).exists():
        time.sleep(.05)
migration.verify_for_activation = paused
service.serve(Path(sys.argv[1]), activate_owner=Path(sys.argv[2]), protected_root=Path(sys.argv[3]),
              protected_keys=Path(sys.argv[4]), activate_verified_inbox=True)
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.config), str(self.owner_path),
            str(self.root / 'inbox'), str(self.root / 'keys'), str(marker), str(release)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        try:
            deadline = time.monotonic() + 30
            while not marker.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('activation did not reach the synthetic barrier')
                time.sleep(.05)
            with closing(sqlite3.connect(self.inbox.root / 'inbox.sqlite3', timeout=.1, isolation_level=None)) as raw:
                with self.assertRaises(sqlite3.OperationalError):
                    raw.execute('BEGIN IMMEDIATE')
            release.write_text('release', encoding='utf-8')
            endpoint = self.config.parent / 'memory-service.json'
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('activation failed after releasing barrier')
                time.sleep(.05)
            self.assertTrue(json.loads(self.config.read_text(encoding='utf-8'))['service_mode'])
        finally:
            if not release.exists():
                release.write_text('release', encoding='utf-8')

    def test_crash_after_mode_write_resumes_with_original_ciphertext(self) -> None:
        linked = self.inbox.submit(self.owner, self.event)
        prepare(self.runtime, self.inbox, self.owner, self.password)
        script = '''import os, sys
from pathlib import Path
import evernook.service as service
original = service._atomic_json
def crash_after_write(path, value):
    original(path, value)
    if path == Path(sys.argv[1]):
        os._exit(91)
service._atomic_json = crash_after_write
service.serve(Path(sys.argv[1]), activate_owner=Path(sys.argv[2]), protected_root=Path(sys.argv[3]),
              protected_keys=Path(sys.argv[4]), activate_verified_inbox=True)
'''
        crashed = subprocess.run([sys.executable, '-B', '-c', script, str(self.config), str(self.owner_path),
            str(self.root / 'inbox'), str(self.root / 'keys')], capture_output=True, timeout=30)
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        self.assertTrue(json.loads(self.config.read_text(encoding='utf-8'))['service_mode'])
        self.assertFalse((self.config.parent / 'memory-service.json').exists())
        with self.assertRaisesRegex(MemoryFailure, 'service_required'):
            self.runtime.submit(self.owner, replace(self.event, source=Source('owner', 'ordinary', 'main', 'old')))
        client = self.start([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--protected-inbox', str(self.root / 'inbox'), '--protected-keys', str(self.root / 'keys')])
        page = client.protected_inventory(self.owner)
        self.assertEqual([(item['object_id'], item['state']) for item in page['objects']],
                         [(linked.object_id, 'protected_linked')])
