from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from evernook import Access, Capture, Event, MemoryFailure, MemoryRuntime, Source
from evernook.protected import ProtectedInbox
from evernook.service import ServiceClient, dispatch
from evernook.vault_local import LocalDisplayRequest
from evernook.vault_multi_proof import prepare
from tests.contracts.test_vault_multi_verify import MultiVaultFixture


class MultiVaultServiceTests(MultiVaultFixture):
    archive_review = True
    def setUp(self) -> None:
        super().setUp()
        self.second.reconcile(self.owner, self.pending.object_id)
        self.second.reconcile(self.owner, self.ack.object_id)
        prepare(self.runtime, self.owner, self.declared, inventory_statement='STOPPED_AND_INVENTORIED')
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=20)

    def command(self, *, activate: bool = False, history: bool = True) -> list[str]:
        command = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                   '--protected-inbox', str(self.first.root), '--protected-keys', str(self.first.key_root)]
        if history:
            command += ['--protected-history-inbox', str(self.second.root), '--protected-history-keys', str(self.second.key_root)]
        if activate:
            command += ['--activate-owner', str(self.owner_file), '--activate-verified-multi']
        return command

    def start(self, *, activate: bool = False) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        old = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen(self.command(activate=activate), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 30
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if process.poll() is not None:
                assert process.stderr is not None
                self.fail('service exited: ' + process.stderr.read().decode(errors='replace'))
            if time.monotonic() >= deadline:
                self.fail('service startup timeout')
            time.sleep(.05)
        return ServiceClient(self.config)

    def test_cutover_routes_both_inboxes_and_rejects_historical_source_collision(self) -> None:
        with self.second._connection() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')]
        client = self.start(activate=True)
        self.assertTrue(client.call(self.owner, 'status', {})['service']['single_writer'])
        self.assertEqual(client.protected_inboxes(self.owner), {'primary': self.first.inbox_id, 'historical': [self.second.inbox_id]})
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.protected_inboxes(self.reader)
        listed = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-protected-inboxes',
                                 '--config', str(self.config), '--access', str(self.owner_file)], capture_output=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)['result']['historical'], [self.second.inbox_id])
        self.assertNotIn(str(self.root).encode(), listed.stdout)
        self.assertEqual(len(client.protected_inventory(self.owner)['objects']), 1)
        self.assertEqual(len(client.protected_inventory(self.owner, inbox_id=self.second.inbox_id)['objects']), 2)
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.protected_inventory(self.reader, inbox_id=self.second.inbox_id)
        with self.assertRaisesRegex(MemoryFailure, 'protected_inbox_not_found'):
            client.protected_inventory(self.owner, inbox_id='00000000-0000-4000-8000-000000000000')
        from evernook.backup import initialize_backup_keys
        backup_keys = self.root / 'synthetic-backup-keys'
        initialize_backup_keys(backup_keys, b'synthetic backup password')
        with self.assertRaisesRegex(MemoryFailure, 'protected_cutover_requires_migration'):
            client.protected_backup(self.owner, self.root / 'not-a-multi-backup', backup_keys)
        self.assertFalse((self.root / 'not-a-multi-backup').exists())
        duplicate = replace(self.event, source=replace(self.event.source, event='two'))
        self.assertEqual(client.protected_receive(self.owner, duplicate)['inbox_id'], self.second.inbox_id)
        streamed = replace(self.event, source=replace(self.event.source, event='three'), text='')
        stream_receipt = client.protected_capture(self.owner, Capture(streamed, 'message', text_chunks=(self.event.text,)))
        self.assertEqual(stream_receipt['status'], 'saved')
        self.assertEqual(stream_receipt['inbox_id'], self.second.inbox_id)
        self.assertEqual(stream_receipt['provenance_status'], 'authenticated')
        with self.assertRaisesRegex(MemoryFailure, 'source_conflict'):
            client.protected_receive(self.owner, replace(duplicate, text='synthetic conflicting retry'))
        fresh = replace(self.event, source=replace(self.event.source, event='fresh'))
        fresh_receipt = client.protected_receive(self.owner, fresh)
        self.assertEqual(fresh_receipt['state'], 'protected_linked')
        self.assertEqual(client.protected_receive(self.owner, fresh)['object_id'], fresh_receipt['object_id'])
        with self.assertRaisesRegex(MemoryFailure, 'service_required'):
            self.runtime.submit(self.owner, replace(self.event, source=replace(self.event.source, event='old-writer')))
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        runtime = MemoryRuntime(self.config, service_owner=True)
        primary = ProtectedInbox(runtime, self.first.root, self.first.key_root)
        history = ProtectedInbox(runtime, self.second.root, self.second.key_root)
        primary._historical_inboxes = (history,)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_provenance'):
            primary.stage(self.owner, duplicate, stream_provenance=1)  # type: ignore[arg-type]
        with history._connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY id')], before)
        for inbox, receipt, password in ((primary, self.linked, self.passwords[0]), (history, self.pending, self.passwords[1])):
            seen: list[str] = []
            LocalDisplayRequest(inbox, self.owner, receipt.object_id, 'synthetic', desktop=lambda: True).display_once(password, seen.append)
            self.assertEqual(seen, [self.event.text])
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            LocalDisplayRequest(history, self.reader, self.pending.object_id, 'synthetic', desktop=lambda: True)
        denied = LocalDisplayRequest(history, self.owner, self.pending.object_id, 'synthetic', desktop=lambda: True)
        with self.assertRaisesRegex(MemoryFailure, 'vault_unlock_refused'):
            denied.display_once(b'synthetic wrong password', lambda _: self.fail('unexpected disclosure'))
        request = {'version': 1, 'access': asdict(self.owner), 'operation': 'protected-show-local',
                   'arguments': {'object_id': self.pending.object_id, 'purpose': 'synthetic', 'inbox_id': history.inbox_id}}
        with patch('evernook.service_vault.show', return_value={'closed': True}) as show:
            import threading
            result = dispatch(runtime, request, protected_inbox=primary, protected_history={history.inbox_id: history},
                              display_gate=threading.BoundedSemaphore(1))
        self.assertEqual(result, {'closed': True})
        self.assertIs(show.call_args.args[0], history)
        calls = 0
        original_scope = runtime._require_scope
        def revoked(*args: object, **kwargs: object) -> object:
            nonlocal calls
            if len(args) >= 3 and args[2] == 'protected_inboxes':
                calls += 1
                if calls == 2:
                    raise MemoryFailure('permission_denied')
            return original_scope(*args, **kwargs)  # type: ignore[arg-type]
        list_request = {'version': 1, 'access': asdict(self.owner), 'operation': 'protected-inboxes', 'arguments': {}}
        with patch.object(runtime, '_require_scope', side_effect=revoked):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                dispatch(runtime, list_request, protected_inbox=primary, protected_history={history.inbox_id: history})
        restarted = self.start()
        self.assertEqual(len(restarted.protected_inventory(self.owner, inbox_id=self.second.inbox_id)['objects']), 2)

    def test_omitted_history_missing_or_tampered_binding_refuses_restart(self) -> None:
        self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        missing = subprocess.run(self.command(history=False), capture_output=True, timeout=30)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(b'protected_multi_binding_mismatch', missing.stderr)
        binding = self.config.parent / 'protected-multi-binding.json'
        original = binding.read_bytes()
        value = json.loads(original)
        value['content']['primary'] = self.second.inbox_id
        binding.write_text(json.dumps(value), encoding='utf-8')
        tampered = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn(b'protected_multi_binding_invalid', tampered.stderr)
        binding.write_bytes(original)
        self.assertEqual(len(self.start().protected_inventory(self.owner)['objects']), 1)

    def test_missing_historical_object_never_stages_a_duplicate_in_primary(self) -> None:
        client = self.start(activate=True)
        with closing(sqlite3.connect(self.second.root / 'inbox.sqlite3')) as db:
            db.execute('DELETE FROM objects WHERE id=?', (self.pending.object_id,))
            db.commit()
        retry = replace(self.event, source=replace(self.event.source, event='two'))
        with self.assertRaisesRegex(MemoryFailure, 'protected_link_unavailable'):
            client.protected_receive(self.owner, retry)
        self.assertEqual(len(client.protected_inventory(self.owner)['objects']), 1)

    def test_missing_primary_object_never_stages_a_replacement(self) -> None:
        client = self.start(activate=True)
        with closing(sqlite3.connect(self.first.root / 'inbox.sqlite3')) as db:
            db.execute('DELETE FROM objects WHERE id=?', (self.linked.object_id,))
            db.commit()
        with self.assertRaisesRegex(MemoryFailure, 'protected_link_unavailable'):
            client.protected_receive(self.owner, self.event)
        with closing(sqlite3.connect(self.first.root / 'inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM objects').fetchone()[0], 0)

    def test_historical_pending_fails_startup_but_primary_pending_can_recover(self) -> None:
        self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        runtime = MemoryRuntime(self.config, service_owner=True)
        primary = ProtectedInbox(runtime, self.first.root, self.first.key_root)
        pending_event = replace(self.event, source=replace(self.event.source, event='primary-pending'))
        staged = primary.stage(self.owner, pending_event)
        self.assertEqual(staged.state, 'protected_pending_link')
        client = self.start()
        self.assertEqual(client.protected_receive(self.owner, pending_event)['object_id'], staged.object_id)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        with closing(sqlite3.connect(self.second.root / 'inbox.sqlite3')) as db:
            db.execute("UPDATE objects SET state='protected_pending_link',receipt=NULL WHERE id=?", (self.ack.object_id,))
            db.commit()
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', refused.stderr)

    @unittest.skipUnless(os.environ.get('EVERNOOK_TEST_OLD_MULTI_PYTHON'), 'old installed Core not supplied')
    def test_old_core_refuses_multi_binding_config(self) -> None:
        self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        result = subprocess.run([os.environ['EVERNOOK_TEST_OLD_MULTI_PYTHON'], '-m', 'evernook.service',
                                 '--config', str(self.config)], env={**os.environ, 'PYTHONPATH': ''},
                                capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'unsupported_config', result.stderr)

    def test_crash_after_binding_before_mode_requires_explicit_retry(self) -> None:
        worker = Path(__file__).with_name('vault_multi_activation_crash_worker.py')
        crash = subprocess.run([sys.executable, '-B', str(worker), str(self.config), str(self.owner_file), str(self.root), 'binding'],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=50)
        self.assertEqual(crash.returncode, 91, crash.stderr)
        refused = subprocess.run(self.command(), capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b'protected_multi_binding_mismatch', refused.stderr)
        self.assertEqual(len(self.start(activate=True).protected_inventory(self.owner, inbox_id=self.second.inbox_id)['objects']), 2)

    def test_tampered_proof_and_omitted_declared_inbox_never_activate(self) -> None:
        bypass = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                                 '--activate-owner', str(self.owner_file), '--activate-restored-without-vault'],
                                capture_output=True, timeout=30)
        self.assertNotEqual(bypass.returncode, 0)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        proof = self.config.parent / 'protected-multi-cutover-proof.json'
        original = proof.read_bytes()
        value = json.loads(original)
        value['snapshot']['objects'] = 0
        proof.write_text(json.dumps(value), encoding='utf-8')
        failed = subprocess.run(self.command(activate=True), capture_output=True, timeout=40)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b'protected_migration_proof_invalid', failed.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        proof.write_bytes(original)
        omitted = subprocess.run(self.command(activate=True, history=False), capture_output=True, timeout=40)
        self.assertNotEqual(omitted.returncode, 0)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))

    def test_lost_config_marker_and_binding_still_refuse_multi_registry(self) -> None:
        self.start(activate=True)
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        binding = self.config.parent / 'protected-multi-binding.json'
        saved_binding, saved_config = binding.read_bytes(), self.config.read_bytes()
        try:
            binding.unlink()
            value = json.loads(saved_config)
            value.pop('protected_multi_binding')
            self.config.write_text(json.dumps(value), encoding='utf-8')
            failed = subprocess.run(self.command(history=False), capture_output=True, timeout=30)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b'protected_cutover_requires_migration', failed.stderr)
        finally:
            binding.write_bytes(saved_binding)
            self.config.write_bytes(saved_config)

    def test_multi_registry_ordinary_recovery_stays_missing_vault_and_readable(self) -> None:
        from evernook.backup import create_backup, initialize_backup_keys
        from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
        client = self.start(activate=True)
        ordinary = replace(self.event, source=Source('owner', 'ordinary', 'main', 'one'),
                           text='synthetic ordinary recovery 232', sensitive=False)
        saved = client.call(self.owner, 'remember', {'event': asdict(ordinary)})
        keys = initialize_backup_keys(self.root / 'ordinary-backup-keys', b'synthetic ordinary backup password')
        backup = self.root / 'ordinary-backup'
        create_backup(MemoryRuntime(self.config, service_owner=True), self.owner, backup, self.root / 'ordinary-backup-keys')
        self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
        destination = self.root / 'ordinary-restored'
        with OfflineRecovery(backup, destination, Path(keys.recovery_file), b'synthetic ordinary backup password') as review:
            review.publish(ACKNOWLEDGEMENT)
        config, owner_path = destination / 'runtime.json', destination / 'mcp-access-owner.json'
        recovered = MemoryRuntime(config)
        access = recovered.access_from_file(owner_path)
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                                    '--activate-owner', str(owner_path), '--activate-restored-without-vault'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        endpoint, deadline = destination / 'memory-service.json', time.monotonic() + 30
        while not endpoint.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                assert process.stderr is not None
                detail = process.stderr.read().decode(errors='replace') if process.poll() is not None else 'timeout'
                self.fail('restored service failed: ' + detail)
            time.sleep(.05)
        restored = ServiceClient(config)
        self.assertEqual(restored.call(access, 'status', {})['service']['protected_state'], 'restored_missing')
        self.assertEqual(restored.call(access, 'read', {'message_id': saved['message_id']})['event']['text'], ordinary.text)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            restored.protected_receive(access, self.event)
        process.terminate(); process.wait(timeout=15)
        previous = endpoint.read_bytes()
        restarted = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(restarted)
        deadline = time.monotonic() + 30
        while endpoint.read_bytes() == previous:
            if restarted.poll() is not None or time.monotonic() >= deadline:
                self.fail('restored missing-vault service failed to restart')
            time.sleep(.05)
        self.assertEqual(ServiceClient(config).call(access, 'status', {})['service']['protected_state'], 'restored_missing')
    def test_crash_after_mode_before_commit_restarts_with_full_binding(self) -> None:
        worker = Path(__file__).with_name('vault_multi_activation_crash_worker.py')
        crash = subprocess.run([sys.executable, '-B', str(worker), str(self.config), str(self.owner_file), str(self.root), 'mode'],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=50)
        self.assertEqual(crash.returncode, 91, crash.stderr)
        self.assertEqual(len(self.start().protected_inventory(self.owner, inbox_id=self.second.inbox_id)['objects']), 2)

    def test_startup_rechecks_multi_marker_after_acquiring_service_lock(self) -> None:
        ready, release = self.root / 'synthetic-ready', self.root / 'synthetic-release'
        script = '''
import sys,time
from pathlib import Path
from unittest.mock import patch
from evernook.models import MemoryFailure
import evernook.service as service
config,root,ready,release=map(Path,sys.argv[1:])
original=service.verify_private_directory
def pause(*args,**kwargs):
    ready.write_text('ready')
    deadline=time.monotonic()+30
    while not release.exists():
        if time.monotonic()>deadline: raise RuntimeError('synthetic timeout')
        time.sleep(.05)
    return original(*args,**kwargs)
try:
    with patch.object(service,'verify_private_directory',side_effect=pause):
        service.serve(config,protected_root=root/'inbox-a',protected_keys=root/'keys-a')
except MemoryFailure as error:
    print(error.code,flush=True)
    raise SystemExit(17)
'''
        paused = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.config), str(self.root), str(ready), str(release)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 25
            while not ready.exists():
                if paused.poll() is not None or time.monotonic() >= deadline:
                    self.fail('synthetic paused startup did not reach the test gate')
                time.sleep(.05)
            self.start(activate=True)
            self.processes[-1].terminate(); self.processes[-1].wait(timeout=15)
            release.write_text('release', encoding='utf-8')
            output, errors = paused.communicate(timeout=30)
            self.assertEqual(paused.returncode, 17, errors)
            self.assertIn(b'protected_multi_binding_mismatch', output)
        finally:
            if paused.poll() is None:
                paused.kill()
                paused.communicate(timeout=10)


if __name__ == '__main__':
    unittest.main()
