from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from nacl import pwhash
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox

from evernook import Event, MemoryRuntime, Source, Transcription, initialize, pair_client
from evernook.models import MemoryFailure
from evernook.protected import initialize_protected_inbox
from evernook.service import ServiceClient
from evernook.service_transport import authenticate_client, receive_json, send_json


class ProtectedServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-protected-service-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'ordinary', client_id='owner', entry='owner')
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path)
        self.reader_path = self.config.parent / 'mcp-access-reader.json'
        self.reader = pair_client(self.config, client_id='reader', entry='reader', access_path=self.reader_path,
                                  scopes=('recall',))
        self.inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys',
                                               b'synthetic service password')
        self.event = Event(Source('owner', 'synthetic-private-session', 'main', '1'), 'user', 1, None,
                           'complete', 'synthetic-private-sentinel-154', sensitive=True)
        self.process: subprocess.Popen[bytes] | None = None
        self.diagnostics: list[bytes] = []
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            if self.process.stderr:
                self.diagnostics.append(self.process.stderr.read())
                self.process.stderr.close()
            self.process = None

    def start(self, *, configured: bool, activate: bool = True) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        old = endpoint.read_bytes() if endpoint.exists() else None
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config)]
        if activate:
            args += ['--activate-owner', str(self.owner_path)]
        if configured:
            args += ['--protected-inbox', str(self.root / 'inbox'), '--protected-keys', str(self.root / 'keys')]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 20
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if self.process.poll() is not None:
                diagnostic = self.process.stderr.read().decode('utf-8', errors='replace') if self.process.stderr else ''
                self.fail('protected service exited: ' + diagnostic)
            if time.monotonic() > deadline:
                self.fail('protected service did not become ready')
            time.sleep(0.05)
        return ServiceClient(self.config)

    def test_private_recording_session_saves_multiple_encrypted_events(self) -> None:
        from evernook.private_session_local import LocalPrivateSession
        client = self.start(configured=True)
        session = LocalPrivateSession(client, self.owner, 'owner')
        secrets = ('synthetic-private-session-first-219', 'synthetic-private-session-second-219')
        for secret in secrets:
            session.submit(secret)
        summary = session.close()
        self.assertEqual(summary['saved_count'], 2)
        self.assertNotIn('synthetic-private-session-', json.dumps(summary))
        for secret in secrets:
            self.assertEqual(client.call(self.owner, 'search', {'query': secret})['hits'], [])
        self.stop()
        contents = b''.join(path.read_bytes() for root in (self.root / 'ordinary', self.root / 'inbox')
                            for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock'))
        for secret in secrets:
            self.assertNotIn(secret.encode(), contents)

    def test_structured_private_capture_reports_gaps_without_submitting_materials(self) -> None:
        from evernook import Capture
        client = self.start(configured=True)
        receipt = client.protected_capture(self.owner, Capture(self.event, 'message'))
        self.assertEqual(receipt['status'], 'saved')
        supported = {'event': asdict(replace(self.event, source=replace(self.event.source, event='cli-supported'))), 'kind': 'message'}
        saved = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-protected-capture', '--config', str(self.config),
            '--access', str(self.owner_path)], input=json.dumps(supported).encode(), capture_output=True, timeout=30)
        self.assertEqual(saved.returncode, 0, saved.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(saved.stdout)['result']['status'], 'saved')
        self.assertNotIn(self.event.text.encode(), saved.stdout + saved.stderr)
        value = {'event': asdict(replace(self.event, source=replace(self.event.source, event='unsupported'))),
                 'kind': 'message', 'attachments': [{'label': 'synthetic-private-label-222',
                 'data': 'synthetic-private-attachment-222'}], 'text_chunks': ['synthetic-private-stream-222']}
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-protected-capture', '--config', str(self.config),
            '--access', str(self.owner_path)], input=json.dumps(value).encode(), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        parsed = json.loads(result.stdout)['result']
        self.assertEqual(parsed['status'], 'not_submitted')
        self.assertEqual(parsed['gaps'], ['private_attachments_unsupported', 'private_stream_unsupported'])
        self.assertNotIn(b'synthetic-private', result.stdout + result.stderr)
        self.assertEqual(client.call(self.owner, 'search', {'query': 'synthetic-private-attachment-222'})['hits'], [])
        self.stop()
        contents = b''.join(path.read_bytes() for root in (self.root / 'ordinary', self.root / 'inbox')
                            for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock'))
        self.assertNotIn(b'synthetic-private-attachment-222', contents)
        self.assertNotIn(b'synthetic-private-stream-222', contents)

    def test_structured_capture_response_loss_after_commit_is_unknown_then_idempotent(self) -> None:
        from evernook import Capture
        client = self.start(configured=True)
        original = client.call
        def committed_then_canceled(*args, **kwargs):  # type: ignore[no-untyped-def]
            original(*args, **kwargs)
            raise MemoryFailure('service_canceled')
        request = Capture(self.event, 'message')
        with patch.object(client, 'call', side_effect=committed_then_canceled):
            result = client.protected_capture(self.owner, request)
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertIsNone(result['protected_original_saved'])
        retry = client.protected_capture(self.owner, request)
        self.assertEqual(retry['status'], 'saved')
        self.assertEqual(len(client.protected_inventory(self.owner)['objects']), 1)

    def test_complete_private_buffered_stream_decrypts_exact_join_and_conflicts_safely(self) -> None:
        from evernook import Capture
        from evernook.protected import ProtectedInbox
        from evernook.vault_crypto import _decrypt_event, _unlock
        client = self.start(configured=True)
        event = replace(self.event, text='')
        request = Capture(event, 'message', text_chunks=('合成🙂', '\r\n', 'synthetic-private-stream-223'))
        receipt = client.protected_capture(self.owner, request)
        self.assertEqual(receipt['status'], 'saved')
        self.assertEqual(receipt['gaps'], ['private_stream_boundaries_not_saved'])
        self.assertEqual(client.protected_capture(self.owner, request)['object_id'], receipt['object_id'])
        changed = replace(request, text_chunks=('different',))
        self.assertEqual(client.protected_capture(self.owner, changed)['status'], 'outcome_unknown')
        cli_event = replace(event, source=replace(event.source, event='cli-buffered'))
        cli_input = {'event': asdict(cli_event), 'kind': 'message', 'text_chunks': ['synthetic-private-', 'cli-stream-223']}
        cli = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-protected-capture', '--config', str(self.config),
            '--access', str(self.owner_path)], input=json.dumps(cli_input).encode(), capture_output=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(cli.stdout)['result']['gaps'], ['private_stream_boundaries_not_saved'])
        self.assertNotIn(b'synthetic-private-', cli.stdout + cli.stderr)
        self.assertEqual(client.call(self.owner, 'search', {'query': 'synthetic-private-stream-223'})['hits'], [])
        self.stop()
        runtime = MemoryRuntime(self.config, service_owner=True)
        inbox = ProtectedInbox(runtime, self.root / 'inbox', self.root / 'keys')
        private = _unlock(inbox, b'synthetic service password')
        with inbox._connection() as db:
            row = db.execute('SELECT * FROM objects WHERE id=?', (receipt['object_id'],)).fetchone()
            self.assertEqual(inbox._receipt(row).version, 2)
            self.assertEqual(_decrypt_event(inbox, row, private).text, '合成🙂\r\nsynthetic-private-stream-223')
        contents = b''.join(path.read_bytes() for root in (self.root / 'ordinary', self.root / 'inbox')
                            for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock'))
        self.assertNotIn(b'synthetic-private-stream-223', contents)
        self.assertNotIn(b'synthetic-private-cli-stream-223', contents)

    def test_legacy_buffered_retry_reports_unknown_provenance(self) -> None:
        from evernook import Capture
        client = self.start(configured=True)
        legacy = client.protected_receive(self.owner, self.event)
        request = Capture(replace(self.event, text=''), 'message', text_chunks=(self.event.text,))
        result = client.protected_capture(self.owner, request)
        self.assertEqual(result['object_id'], legacy['object_id'])
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['provenance_status'], 'legacy_unknown')
        self.assertEqual(result['gaps'], ['private_stream_boundaries_not_saved'])

    @unittest.skipUnless(os.environ.get('EVERNOOK_TEST_082_PYTHON'), 'Core 0.1.82 not supplied')
    def test_new_stream_client_fails_closed_against_082_service(self) -> None:
        from evernook import Capture
        endpoint = self.config.parent / 'memory-service.json'
        self.process = subprocess.Popen([os.environ['EVERNOOK_TEST_082_PYTHON'], '-B', '-m', 'evernook.service',
            '--config', str(self.config), '--activate-owner', str(self.owner_path),
            '--protected-inbox', str(self.root / 'inbox'), '--protected-keys', str(self.root / 'keys')],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=''))
        deadline = time.monotonic() + 20
        while not endpoint.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.fail('synthetic Core 0.1.82 service did not start')
            time.sleep(.05)
        client = ServiceClient(self.config)
        request = Capture(replace(self.event, text=''), 'message', text_chunks=(self.event.text,))
        self.assertEqual(client.protected_capture(self.owner, request)['status'], 'outcome_unknown')
        self.assertEqual(client.protected_inventory(self.owner)['objects'], [])
        self.assertEqual(client.protected_receive(self.owner, self.event)['version'], 1)

    def test_private_session_cli_rejects_invalid_resume_before_window(self) -> None:
        self.start(configured=True)
        result = subprocess.run([sys.executable, '-m', 'evernook.cli', 'service-private-session',
            '--config', str(self.config), '--access', str(self.owner_path), '--entry', 'owner', '--sequence', '100'],
            capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid_private_source', result.stdout)

    def test_explicit_service_capture_encrypts_and_never_returns_plaintext(self) -> None:
        client = self.start(configured=True)
        self.assertTrue(client.call(self.owner, 'status', {})['service']['protected_capture'])
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.reader, 'protected-receive', {'event': asdict(self.event)})
        other = asdict(self.event)
        other['source']['entry'] = 'reader'
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            client.call(self.owner, 'protected-receive', {'event': other})
        unmarked = asdict(self.event)
        unmarked['sensitive'] = False
        with self.assertRaisesRegex(MemoryFailure, 'protected_explicit_mark_required'):
            client.call(self.owner, 'protected-receive', {'event': unmarked})
        incomplete = asdict(self.event)
        incomplete['completion'] = 'partial'
        with self.assertRaisesRegex(MemoryFailure, 'protected_incomplete_unsupported'):
            client.call(self.owner, 'protected-receive', {'event': incomplete})
        import evernook.service as service_module
        transmitted: list[object] = []
        received: list[object] = []
        send, receive = send_json, receive_json
        def capture_send(connection: socket.socket, value: object, deadline: float) -> None:
            transmitted.append(value)
            send(connection, value, deadline)
        def capture_receive(connection: socket.socket, deadline: float, cancel: object = None) -> object:
            value = receive(connection, deadline, cancel)  # type: ignore[arg-type]
            received.append(value)
            return value
        with patch.object(service_module, 'send_json', side_effect=capture_send), \
                patch.object(service_module, 'receive_json', side_effect=capture_receive):
            first = client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        wire = json.dumps([transmitted, received], ensure_ascii=False)
        self.assertNotIn('synthetic-private-sentinel-154', wire)
        self.assertNotIn(self.owner.token, wire)
        self.assertIn('sealed', wire)
        again = client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        self.assertEqual(first, again)
        self.assertEqual(first['state'], 'protected_linked')
        self.assertTrue(first['encrypted_original_saved'])
        self.assertFalse(first['message']['original_saved'])
        self.assertNotIn('synthetic-private-sentinel-154', json.dumps(first))
        tools = client.call(self.owner, 'mcp-list', {'profile': 'trusted-host'})['tools']
        self.assertNotIn('protected-receive', [item['name'] for item in tools])
        self.assertNotIn('protected-inventory', [item['name'] for item in tools])
        self.assertNotIn('protected-links', [item['name'] for item in tools])
        read = client.call(self.owner, 'read', {'message_id': first['message']['message_id']})
        self.assertNotIn('synthetic-private-sentinel-154', json.dumps(read))
        self.assertEqual(client.call(self.owner, 'search', {'query': 'synthetic-private-sentinel-154'})['hits'], [])
        cli_event = asdict(self.event)
        cli_event['source']['event'] = 'cli'
        cli_event['text'] = 'password=synthetic-vault-password-154'
        cli_result = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'service-protected-receive',
            '--config', str(self.config), '--access', str(self.owner_path)],
            input=json.dumps(cli_event).encode('utf-8'), capture_output=True, timeout=30)
        self.assertEqual(cli_result.returncode, 0, cli_result.stdout)
        self.assertEqual(json.loads(cli_result.stdout)['result']['state'], 'protected_linked')
        self.assertNotIn(b'synthetic-vault-password-154', cli_result.stdout + cli_result.stderr)
        self.stop()
        client = self.start(configured=True, activate=False)
        self.assertEqual(client.call(self.owner, 'protected-receive', {'event': asdict(self.event)}), first)
        self.stop()
        recovery = json.loads((self.root / 'keys/recovery.json').read_text(encoding='utf-8'))
        derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, b'synthetic service password',
            bytes.fromhex(recovery['salt']), opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE,
            memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        private = json.loads(SecretBox(derived).decrypt(bytes.fromhex(recovery['ciphertext'])))
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            envelope = json.loads(db.execute('SELECT envelope FROM objects WHERE id=?', (first['object_id'],)).fetchone()[0])
        clear = json.loads(SealedBox(PrivateKey(bytes.fromhex(private['private']))).decrypt(bytes.fromhex(envelope['ciphertext'])))
        self.assertEqual(clear['event'], asdict(self.event))
        contents = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(b'synthetic-private-sentinel-154', contents)
        self.assertNotIn(b'synthetic-vault-password-154', contents)
        self.assertNotIn(b'synthetic-private-sentinel-154', b''.join(self.diagnostics))

    def test_unconfigured_service_refuses_protected_capture(self) -> None:
        client = self.start(configured=False)
        self.assertFalse(client.call(self.owner, 'status', {})['service']['protected_capture'])
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.call(self.owner, 'protected-receive', {'event': asdict(self.event)})
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM objects').fetchone()[0], 0)

    def test_unconfigured_service_never_falls_back_for_private_transcription(self) -> None:
        client = self.start(configured=False)
        private = Transcription(Source('owner', 'synthetic-voice', 'voice', 'private'), 1, None, 'complete',
            'synthetic-no-vault-secret-168', 'synthetic-asr', 'model-v1', 'en-US', 'local', 0, 100, sensitive=True)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.submit_transcription(self.owner, private)
        self.assertEqual(client.call(self.owner, 'search', {'query': private.text})['hits'], [])
        self.stop()
        files = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(private.text.encode(), files)

    def test_transcription_service_seals_private_material_and_exposes_only_ordinary_provenance(self) -> None:
        client = self.start(configured=True)
        ordinary = Transcription(Source('owner', 'asr-session', 'voice', '1'), 1, None, 'complete',
            'synthetic ordinary walking transcript', 'synthetic-asr', 'model-v1', 'en-US', 'local', 0, 500, 0.8)
        canceled = threading.Event(); canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'service_canceled'):
            client.submit_transcription(self.owner, ordinary, cancel=canceled)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)
        public = client.submit_transcription(self.owner, ordinary)
        self.assertEqual(public['storage'], 'ordinary')
        message_id = public['receipt']['message']['message_id']
        self.assertEqual(client.read_transcription(self.owner, message_id)['transcription']['confirmation'], 'unverified')
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.submit_transcription(self.reader, ordinary)
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            client.submit_transcription(self.owner, replace(ordinary, source=replace(ordinary.source, entry='reader')))
        with self.assertRaisesRegex(MemoryFailure, 'source_conflict'):
            client.submit_transcription(self.owner, replace(ordinary, model='model-v2'))
        endpoint = json.loads((self.config.parent / 'memory-service.json').read_text(encoding='utf-8'))
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
            deadline = time.monotonic() + 5
            authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
            send_json(connection, {'version': 1, 'access': asdict(self.owner), 'operation': 'transcription-submit',
                'arguments': {'material': asdict(ordinary)}}, deadline)
            self.assertEqual(receive_json(connection, deadline)['error']['code'], 'protected_encryption_required')
        private = Transcription(Source('owner', 'asr-session', 'voice', 'private'), 2, None, 'complete',
            'synthetic-private-asr-sentinel-168', 'synthetic-asr', 'model-v1', 'en-US', 'remote', 0, 500, None, True)
        import evernook.service as service_module
        sent: list[object] = []
        def capture_send(connection: socket.socket, value: object, deadline: float) -> None:
            sent.append(value); send_json(connection, value, deadline)
        with patch.object(service_module, 'send_json', side_effect=capture_send):
            protected = client.submit_transcription(self.owner, private)
        self.assertNotIn(private.text, json.dumps(sent))
        self.assertNotIn(private.text, json.dumps(protected))
        self.assertEqual(protected['receipt']['state'], 'protected_linked')
        self.assertFalse(protected['receipt']['message']['original_saved'])
        self.assertEqual(client.submit_transcription(self.owner, private), protected)
        self.assertIsNone(client.read_transcription(self.owner, protected['receipt']['message']['message_id']))
        self.assertEqual(client.call(self.owner, 'search', {'query': private.text})['hits'], [])
        with self.assertRaisesRegex(MemoryFailure, 'protected_incomplete_unsupported'):
            client.submit_transcription(self.owner, replace(private, completion='partial'))
        cli = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'service-transcription-submit',
            '--config', str(self.config), '--access', str(self.owner_path)], input=json.dumps(asdict(ordinary)).encode(),
            capture_output=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stdout)
        self.assertEqual(json.loads(cli.stdout)['result'], public)
        read_cli = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', 'service-transcription-read',
            '--config', str(self.config), '--access', str(self.owner_path), '--message-id', message_id],
            capture_output=True, timeout=30)
        self.assertEqual(read_cli.returncode, 0, read_cli.stdout)
        self.assertEqual(json.loads(read_cli.stdout)['result']['transcription']['confirmation'], 'unverified')
        self.stop()
        recovery = json.loads((self.root / 'keys/recovery.json').read_text(encoding='utf-8'))
        derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, b'synthetic service password', bytes.fromhex(recovery['salt']),
            opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        key = json.loads(SecretBox(derived).decrypt(bytes.fromhex(recovery['ciphertext'])))
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            envelope = json.loads(db.execute('SELECT envelope FROM objects WHERE id=?', (protected['receipt']['object_id'],)).fetchone()[0])
        clear = json.loads(SealedBox(PrivateKey(bytes.fromhex(key['private']))).decrypt(bytes.fromhex(envelope['ciphertext'])))
        payload = json.loads(clear['event']['text'])
        self.assertEqual(payload['text'], private.text)
        self.assertEqual(payload['metadata']['confirmation'], 'unverified')
        files = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(private.text.encode(), files)

    def test_local_capture_state_machine_uses_encrypted_service(self) -> None:
        from evernook.private_capture_local import LocalPrivateCapture
        client = self.start(configured=True)
        capture = LocalPrivateCapture(client, self.owner, 'owner')
        capture.submit('synthetic-private-window-note-156')
        summary = capture.close()
        self.assertEqual(summary['state'], 'saved')
        self.assertNotIn('synthetic-private-window-note-156', json.dumps(summary))
        self.assertEqual(client.call(self.owner, 'search', {'query': 'synthetic-private-window-note-156'})['hits'], [])
        self.stop()
        contents = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(b'synthetic-private-window-note-156', contents)

    def test_plaintext_protected_request_is_rejected_before_persistence(self) -> None:
        client = self.start(configured=True)
        endpoint = json.loads((self.config.parent / 'memory-service.json').read_text(encoding='utf-8'))
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
            deadline = time.monotonic() + 5
            authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
            send_json(connection, {'version': 1, 'access': asdict(self.owner), 'operation': 'protected-receive',
                                   'arguments': {'event': asdict(self.event)}}, deadline)
            response = receive_json(connection, deadline)
        self.assertEqual(response['error']['code'], 'protected_encryption_required')
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    def test_service_inventory_is_encrypted_admin_only_paginated_and_metadata_only(self) -> None:
        client = self.start(configured=True)
        first = client.protected_receive(self.owner, self.event)
        other = asdict(self.event)
        other['source']['event'] = 'second'
        other['text'] = 'synthetic-inventory-secret-164'
        second = client.call(self.owner, 'protected-receive', {'event': other})
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.protected_inventory(self.reader)
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.protected_links(self.reader)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_page'):
            client.protected_inventory(self.owner, limit=101)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_id'):
            client.protected_links(self.owner, after='not-a-cursor')
        import evernook.service as service_module
        sent: list[object] = []
        received: list[object] = []
        def capture_send(connection: socket.socket, value: object, deadline: float) -> None:
            sent.append(value); send_json(connection, value, deadline)
        def capture_receive(connection: socket.socket, deadline: float, cancel: object = None) -> object:
            value = receive_json(connection, deadline, cancel)  # type: ignore[arg-type]
            received.append(value); return value
        with patch.object(service_module, 'send_json', side_effect=capture_send), \
                patch.object(service_module, 'receive_json', side_effect=capture_receive):
            client.protected_inventory(self.owner)
            client.protected_links(self.owner)
        self.assertTrue(all('sealed' in value for value in (*sent, *received)))
        wire = json.dumps([sent, received])
        self.assertNotIn(self.owner.token, wire)
        self.assertNotIn(first['object_id'], wire)
        pages = []
        page = client.protected_inventory(self.owner, limit=1)
        pages.extend(page['objects'])
        self.assertIsNotNone(page['next_cursor'])
        page = client.protected_inventory(self.owner, after=page['next_cursor'], limit=1)
        pages.extend(page['objects'])
        self.assertIsNone(page['next_cursor'])
        self.assertEqual({item['object_id'] for item in pages}, {first['object_id'], second['object_id']})
        links = client.protected_links(self.owner)
        self.assertEqual({item['state'] for item in links['links']}, {'linked_verified'})
        self.assertNotIn('synthetic-inventory-secret-164', json.dumps([pages, links]))
        for operation in ('protected-inventory', 'protected-links'):
            endpoint = json.loads((self.config.parent / 'memory-service.json').read_text(encoding='utf-8'))
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
                deadline = time.monotonic() + 5
                authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
                send_json(connection, {'version': 1, 'access': asdict(self.owner), 'operation': operation,
                                       'arguments': {'after': None, 'limit': 100}}, deadline)
                self.assertEqual(receive_json(connection, deadline)['error']['code'], 'protected_encryption_required')
        for name in ('service-protected-inventory', 'service-protected-links'):
            result = subprocess.run([sys.executable, '-B', '-m', 'evernook.cli', name, '--config', str(self.config),
                '--access', str(self.owner_path)], input=b'{}', capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertNotIn(b'synthetic-inventory-secret-164', result.stdout + result.stderr)
            self.assertNotIn(self.owner.token.encode(), result.stdout + result.stderr)
        with closing(sqlite3.connect(self.root / 'inbox' / 'inbox.sqlite3')) as db:
            db.execute("UPDATE objects SET envelope='{}' WHERE id=?", (first['object_id'],))
            db.commit()
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            client.protected_inventory(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'protected_object_corrupt'):
            client.protected_links(self.owner)

    def test_service_inventory_fails_when_inbox_unconfigured(self) -> None:
        client = self.start(configured=False)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.protected_inventory(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.protected_links(self.owner)
        with self.assertRaisesRegex(MemoryFailure, 'protected_service_unconfigured'):
            client.protected_show_local(self.owner, '00000000-0000-4000-8000-000000000000', 'synthetic')

    def test_service_local_display_rejects_other_entry_and_plaintext_transport(self) -> None:
        client = self.start(configured=True)
        receipt = client.protected_receive(self.owner, self.event)
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            client.protected_show_local(self.reader, receipt['object_id'], 'synthetic local review')
        endpoint = json.loads((self.config.parent / 'memory-service.json').read_text(encoding='utf-8'))
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
            deadline = time.monotonic() + 5
            authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
            send_json(connection, {'version': 1, 'access': asdict(self.owner), 'operation': 'protected-show-local',
                'arguments': {'object_id': receipt['object_id'], 'purpose': 'synthetic'}}, deadline)
            self.assertEqual(receive_json(connection, deadline)['error']['code'], 'protected_encryption_required')

    def test_missing_client_crypto_never_sends_private_request(self) -> None:
        client = self.start(configured=True)
        with patch('evernook.service_transport._private_box', side_effect=MemoryFailure('vault_dependency_missing')), \
                patch('evernook.service.send_json') as send:
            with self.assertRaisesRegex(MemoryFailure, 'vault_dependency_missing'):
                client.protected_receive(self.owner, self.event)
            with self.assertRaisesRegex(MemoryFailure, 'vault_dependency_missing'):
                client.protected_inventory(self.owner)
            with self.assertRaisesRegex(MemoryFailure, 'vault_dependency_missing'):
                client.protected_links(self.owner)
            with self.assertRaisesRegex(MemoryFailure, 'vault_dependency_missing'):
                client.submit_transcription(self.owner, Transcription(Source('owner', 'synthetic', 'voice', 'missing-crypto'),
                    1, None, 'complete', 'synthetic-asr-no-fallback', 'synthetic', 'model-v1', 'en-US', 'local', 0, 1))
            send.assert_not_called()
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    def test_client_encryption_failure_never_sends_or_echoes_private_request(self) -> None:
        client = self.start(configured=True)
        class BrokenBox:
            def encrypt(self, value: bytes) -> bytes:
                raise RuntimeError('synthetic-private-sentinel-154')
        with patch('evernook.service_transport._private_box', return_value=BrokenBox()), \
                patch('evernook.service.send_json') as send:
            with self.assertRaisesRegex(MemoryFailure, '^protected_encryption_failed$') as failure:
                client.protected_receive(self.owner, self.event)
            self.assertNotIn('synthetic-private-sentinel-154', str(failure.exception))
            send.assert_not_called()
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    def test_plaintext_response_from_older_peer_never_causes_fallback(self) -> None:
        client = self.start(configured=False)
        with patch('evernook.service.receive_json', return_value={'ok': False, 'error': {'code': 'invalid_service_request'}}), \
                patch('evernook.service.send_json', wraps=send_json) as send:
            with self.assertRaisesRegex(MemoryFailure, 'protected_encryption_required'):
                client.protected_receive(self.owner, self.event)
            self.assertEqual(send.call_count, 1)
            wire = json.dumps(send.call_args.args[1], ensure_ascii=False)
            self.assertNotIn('synthetic-private-sentinel-154', wire)
            self.assertNotIn(self.owner.token, wire)

    @unittest.skipUnless(os.environ.get('EVERNOOK_TEST_PREVIOUS_PYTHON'), 'previous installed service not supplied')
    def test_actual_previous_service_never_receives_plaintext_from_new_client(self) -> None:
        previous = os.environ['EVERNOOK_TEST_PREVIOUS_PYTHON']
        endpoint = self.config.parent / 'memory-service.json'
        env = dict(os.environ, PYTHONPATH='')
        self.process = subprocess.Popen([previous, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'keys')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        deadline = time.monotonic() + 20
        while not endpoint.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.fail('previous synthetic service did not start')
            time.sleep(0.05)
        client = ServiceClient(self.config)
        with patch('evernook.service.send_json', wraps=send_json) as send:
            with self.assertRaisesRegex(MemoryFailure, 'protected_encryption_required'):
                client.protected_receive(self.owner, self.event)
            self.assertEqual(send.call_count, 1)
            wire = json.dumps(send.call_args.args[1], ensure_ascii=False)
            self.assertNotIn('synthetic-private-sentinel-154', wire)
            self.assertNotIn(self.owner.token, wire)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    @unittest.skipUnless(os.environ.get('EVERNOOK_TEST_PREVIOUS_PYTHON'), 'previous installed client not supplied')
    def test_actual_previous_client_is_rejected_by_new_service(self) -> None:
        client = self.start(configured=True)
        result = subprocess.run([os.environ['EVERNOOK_TEST_PREVIOUS_PYTHON'], '-B', '-m', 'evernook.cli',
            'service-protected-receive', '--config', str(self.config), '--access', str(self.owner_path)],
            input=json.dumps(asdict(self.event)).encode('utf-8'), capture_output=True, timeout=30,
            env=dict(os.environ, PYTHONPATH=''))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'protected_encryption_required')
        self.assertNotIn(b'synthetic-private-sentinel-154', result.stdout + result.stderr)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    @unittest.skipUnless(os.environ.get('EVERNOOK_TEST_NO_VAULT_PYTHON'), 'client without vault extra not supplied')
    def test_actual_client_without_crypto_fails_before_private_send(self) -> None:
        client = self.start(configured=True)
        python = os.environ['EVERNOOK_TEST_NO_VAULT_PYTHON']
        env = dict(os.environ, PYTHONPATH='')
        check = subprocess.run([python, '-c', 'import importlib.util; assert importlib.util.find_spec("nacl") is None'],
                               capture_output=True, timeout=15, env=env)
        self.assertEqual(check.returncode, 0, check.stderr)
        result = subprocess.run([python, '-B', '-m', 'evernook.cli', 'service-protected-receive',
            '--config', str(self.config), '--access', str(self.owner_path)],
            input=json.dumps(asdict(self.event)).encode('utf-8'), capture_output=True, timeout=30, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'vault_dependency_missing')
        self.assertNotIn(b'synthetic-private-sentinel-154', result.stdout + result.stderr)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    def test_tampered_and_cross_session_private_frames_are_rejected(self) -> None:
        from evernook.service_transport import open_private_frame, seal_private_frame
        client = self.start(configured=True)
        endpoint = json.loads((self.config.parent / 'memory-service.json').read_text(encoding='utf-8'))
        request = {'version': 1, 'access': asdict(self.owner), 'operation': 'protected-receive',
                   'arguments': {'event': asdict(self.event)}}
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
            deadline = time.monotonic() + 5
            session = authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
            frame = seal_private_frame(session, request, direction='request')
            with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_frame'):
                open_private_frame(session, frame, direction='response')
            tampered = dict(frame)
            cipher = tampered['sealed']
            tampered['sealed'] = ('0' if cipher[0] != '0' else '1') + cipher[1:]
            send_json(connection, tampered, deadline)
            self.assertEqual(receive_json(connection, deadline)['error']['code'], 'invalid_protected_frame')
        with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as connection:
            deadline = time.monotonic() + 5
            authenticate_client(connection, bytes.fromhex(endpoint['key']), deadline)
            send_json(connection, frame, deadline)
            self.assertEqual(receive_json(connection, deadline)['error']['code'], 'invalid_protected_frame')
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)

    def test_activation_refuses_existing_pending_inbox(self) -> None:
        self.inbox.stage(self.owner, self.event)
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'keys')], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_cutover_requires_migration', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertEqual(len(self.inbox.pending(self.owner)), 1)

    def test_incomplete_service_configuration_refuses_before_activation(self) -> None:
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox')],
            capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid_protected_service_config', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertFalse((self.config.parent / 'memory-service.json').exists())

    def test_mismatched_inbox_material_refuses_before_activation(self) -> None:
        other_config, _ = initialize(self.root / 'other-ordinary', client_id='other', entry='other')
        initialize_protected_inbox(MemoryRuntime(other_config), self.root / 'other-inbox',
                                   self.root / 'other-keys', b'synthetic other password')
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
            '--protected-keys', str(self.root / 'other-keys')], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'protected_binding_mismatch', result.stderr)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))
        self.assertFalse((self.config.parent / 'memory-service.json').exists())

    def test_missing_writer_material_fails_without_plaintext_fallback(self) -> None:
        client = self.start(configured=True)
        (self.root / 'keys/writer.key').unlink()
        with self.assertRaisesRegex(MemoryFailure, 'invalid_protected_material'):
            client.protected_receive(self.owner, self.event)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)
        self.stop()
        contents = b''.join(path.read_bytes() for path in self.root.rglob('*') if path.is_file())
        self.assertNotIn(b'synthetic-private-sentinel-154', contents)

    def test_failed_core_link_survives_restart_and_retry(self) -> None:
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3', isolation_level=None)) as db:
            db.execute('CREATE TABLE protected_links_v1 (message_id TEXT PRIMARY KEY REFERENCES messages(id), reference TEXT NOT NULL)')
            db.execute("CREATE TRIGGER synthetic_link_failure BEFORE INSERT ON protected_links_v1 BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        client = self.start(configured=True)
        with self.assertRaises(MemoryFailure):
            client.protected_receive(self.owner, self.event)
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 0)
        self.stop()
        with closing(sqlite3.connect(self.root / 'inbox/inbox.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM objects WHERE state='protected_pending_link'").fetchone()[0], 1)
        with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3', isolation_level=None)) as db:
            db.execute('DROP TRIGGER synthetic_link_failure')
        client = self.start(configured=True, activate=False)
        self.assertEqual(client.protected_receive(self.owner, self.event)['state'], 'protected_linked')
        self.assertEqual(client.call(self.owner, 'status', {})['ledger']['messages'], 1)

    def test_staging_holds_cutover_gate_until_pending_object_is_visible(self) -> None:
        ready, release = threading.Event(), threading.Event()
        original = self.inbox._stage_authorized
        def paused(entry: str, event: Event):  # type: ignore[no-untyped-def]
            ready.set()
            if not release.wait(15):
                raise RuntimeError('synthetic race timeout')
            return original(entry, event)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(self.inbox, '_stage_authorized', side_effect=paused):
                staged = pool.submit(self.inbox.stage, self.owner, self.event)
                self.assertTrue(ready.wait(10))
                process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
                    '--activate-owner', str(self.owner_path), '--protected-inbox', str(self.root / 'inbox'),
                    '--protected-keys', str(self.root / 'keys')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    try:
                        time.sleep(2)
                        self.assertIsNone(process.poll())
                        self.assertFalse((self.config.parent / 'memory-service.json').exists())
                    finally:
                        release.set()
                    self.assertEqual(staged.result(timeout=15).state, 'protected_pending_link')
                    _, stderr = process.communicate(timeout=20)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(b'protected_cutover_requires_migration', stderr)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=15)
        self.assertNotIn('service_mode', json.loads(self.config.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
