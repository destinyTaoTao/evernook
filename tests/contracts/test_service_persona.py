from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import ctypes
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.service import ServiceClient
from probes.admission.experiment_job import ExperimentJob


def _wire_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict) and all(isinstance(key, str) for key in value)
    return value


def _wire_rows(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_wire_object(item) for item in value]


class ServicePersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-service-persona-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'data', client_id='owner', entry='owner')
        self.owner_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path)
        self.ordinary = pair_client(self.config, client_id='ordinary', entry='ordinary',
            access_path=self.config.parent / 'mcp-access-ordinary.json', scopes=('capture', 'recall'))
        runtime = MemoryRuntime(self.config)
        self.evidence = runtime.submit(self.owner, Event(Source('owner', 's', 'main', '1'), 'user', 1, None,
            'complete', '合成：请设置温和语气。')).message_id
        self.assistant = runtime.submit(self.owner, Event(Source('owner', 's', 'main', '2'), 'assistant', 2, None,
            'complete', '合成：我猜应设置活泼语气。')).message_id
        self.service_job: ExperimentJob | None = None

    def stop_service(self, job: ExperimentJob) -> None:
        if job.handle is None:
            return
        with ExitStack() as cleanup:
            cleanup.callback(job.close)
            process = job.process
            if process is None:
                return
            handles: list[int] = []
            for pid in job.pids():
                handle: int = job.kernel.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION.
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # A member can exit between listing it and opening a handle.
                        continue
                    raise ctypes.WinError(error)
                cleanup.callback(job.kernel.CloseHandle, handle)
                if job.contains(handle):
                    handles.append(handle)
            deadline = time.monotonic() + 15
            job.terminate(process)
            # The launcher can be gone while the actual service still owns memory-service.lock.
            for handle in handles:
                status = job.kernel.WaitForSingleObject(handle, max(0, math.ceil((deadline - time.monotonic()) * 1000)))
                if status == 258:
                    self.fail('owned service member did not exit before cleanup')
                if status != 0:
                    raise ctypes.WinError(ctypes.get_last_error())

    def start(self, *, activate: bool = False) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        old = endpoint.read_bytes() if endpoint.exists() else None
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config)]
        if activate:
            args += ['--activate-owner', str(self.owner_path)]
        job = ExperimentJob()
        self.service_job = job
        self.addCleanup(self.stop_service, job)
        diagnostic = self.root / 'service.stderr'
        with open(os.devnull, 'wb') as stdout, diagnostic.open('wb') as stderr:
            process = job.start(args, stdout, stderr)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('isolated service failed to start: ' + diagnostic.read_text(encoding='utf-8', errors='replace'))
            if endpoint.exists() and endpoint.read_bytes() != old:
                client = ServiceClient(self.config)
                client.call(self.owner, 'status', {})
                return client
            time.sleep(0.05)
        self.fail('isolated service readiness timeout')

    def test_activated_service_persona_cli_and_api_preserve_authority_and_versions(self) -> None:
        client = self.start(activate=True)
        def call(command: str, value: dict[str, object]) -> dict[str, object]:
            return _wire_object(client.call(self.owner, 'cli', {'command': command, 'input': value}))
        def cli(command: str, value: dict[str, object]) -> dict[str, object]:
            result = subprocess.run([sys.executable, '-m', 'evernook.cli', command, '--config', str(self.config),
                '--access', str(self.owner_path), '--require-service'],
                input=json.dumps(value, ensure_ascii=False).encode(), capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            envelope = _wire_object(json.loads(result.stdout))
            return _wire_object(envelope['result'])
        first = {'request_id': 'persona-one', 'block': 'tone', 'text': '合成：温和',
                 'evidence_id': self.evidence, 'reason': '合成：已确认', 'expected_revision': 0}
        saved = call('persona-set', first)
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(cli('persona-set', first), saved)
        projection = client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'read-versioned', 'view': 'persona'}})
        self.assertEqual(projection['value']['items'][0]['text'], '合成：温和')
        graph = client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'read-versioned', 'view': 'network_graph'}})
        self.assertEqual([(node['kind'], node['id'], node['edges'][0]['role']) for node in graph['value']['nodes']],
                         [('persona', 'tone', 'user')])
        self.assertNotIn('合成：已确认', json.dumps(graph, ensure_ascii=False))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', {'command': 'manage', 'input': {'action': 'read', 'view': 'persona'}})
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', {'command': 'manage', 'input': {'action': 'read', 'view': 'network_graph'}})
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', {'command': 'persona-set', 'input': {**first, 'request_id': 'unauthorized'}})
        with self.assertRaisesRegex(MemoryFailure, 'persona_revision_conflict'):
            call('persona-set', {**first, 'request_id': 'stale'})
        with self.assertRaisesRegex(MemoryFailure, 'protected_persona'):
            call('persona-set', {**first, 'request_id': 'secret', 'expected_revision': 1, 'text': 'password=synthetic-secret-123'})
        with self.assertRaisesRegex(MemoryFailure, 'persona_user_confirmation_required'):
            call('persona-set', {**first, 'request_id': 'assistant', 'expected_revision': 1, 'evidence_id': self.assistant})
        updated = call('persona-set', {**first, 'request_id': 'persona-two', 'text': '合成：活泼', 'expected_revision': 1})
        graph_after = client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'fingerprint', 'view': 'network_graph'}})
        self.assertNotEqual(graph['token'], graph_after['token'])
        fingerprint = client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'fingerprint', 'view': 'persona'}})
        self.assertNotEqual(projection['token'], fingerprint['token'])
        history = cli('persona-history', {'block': 'tone'})
        self.assertEqual([record['revision'] for record in _wire_rows(history['records'])], [1, 2])
        target_seq, expected_seq = saved['seq'], updated['seq']
        assert type(target_seq) is int and type(expected_seq) is int
        rollback = cli('persona-rollback', {'request_id': 'persona-rollback', 'target_seq': target_seq,
            'expected_seq': expected_seq, 'evidence_id': self.evidence, 'reason': '合成：回到原设定'})
        self.assertEqual(_wire_rows(rollback['restored'])[0]['text'], '合成：温和')
        tools = client.call(self.owner, 'mcp-list', {'profile': 'trusted-host'})['tools']
        self.assertFalse(any('persona' in tool['name'] for tool in tools))
        with self.assertRaises(MemoryFailure) as rejected:
            client.call(self.owner, 'mcp', {'profile': 'trusted-host', 'name': 'persona-set', 'arguments': first})
        self.assertIn(rejected.exception.code, {'operation_not_allowed', 'unknown_tool'})
        blocks = _wire_object(cli('persona-read', {})['blocks'])
        self.assertEqual(_wire_object(blocks['tone'])['text'], '合成：温和')
        assert self.service_job is not None
        self.stop_service(self.service_job)
        client = self.start()
        read = client.call(self.ordinary, 'cli', {'command': 'persona-read', 'input': {}})
        self.assertEqual(read['blocks']['tone']['text'], '合成：温和')
        self.assertEqual(read['blocks']['tone']['revision'], 3)

    def test_activated_service_graph_exposes_only_valid_episode_candidate(self) -> None:
        from dataclasses import asdict
        client = self.start(activate=True)
        second = client.call(self.owner, 'remember', {'event': asdict(Event(
            Source('owner', 'another-session', 'main', 'episode'), 'assistant', 1, None,
            'complete', '合成：后来记录了讨论。'))})['message_id']
        candidate = client.call(self.owner, 'cli', {'command': 'episode-propose',
            'input': {'source_ids': [self.evidence, second], 'topic': '合成待审经历'}})
        graph = {'command': 'manage', 'input': {'action': 'read', 'view': 'network_graph'}}
        node, = client.call(self.owner, 'cli', graph)['nodes']
        self.assertEqual((node['id'], node['state'], node['revision']), (candidate['candidate_id'], 'pending_review', None))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', graph)
        operation = {'kind': 'source_suppress', 'message_id': self.evidence}
        preview = client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'preview', 'operation': operation}})
        client.call(self.owner, 'cli', {'command': 'manage', 'input': {'action': 'confirm', 'proposal_id': preview['proposal_id'], 'operation': operation}})
        self.assertEqual(client.call(self.owner, 'cli', graph)['nodes'], [])

    def test_service_profile_history_uses_source_visibility(self) -> None:
        from dataclasses import asdict
        from evernook import Change, EvidenceSpan
        from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry

        runtime = MemoryRuntime(self.config)
        text = '合成：请设置温和语气。'
        span = EvidenceSpan(self.evidence, 1, 'original', 1, 0, len(text), text)
        profiles = ProfileRegistry(runtime)
        profiles.decide(self.owner, ProfileDecision('profile-one', 'service-tag', 0, 'propose',
            ProfileDraft('interest', '温和语气', 0.8, (span,))))
        control = runtime.submit(self.owner, Event(Source('owner', 's', 'main', '3'), 'user', 3, None,
            'complete', '合成：隐藏来源')).message_id
        client = self.start(activate=True)
        args = {'command': 'profile-history', 'input': {'tag_id': 'service-tag'}}
        self.assertEqual(len(client.call(self.ordinary, 'cli', args)['records']), 1)
        cli = subprocess.run([sys.executable, '-m', 'evernook.cli', 'profile-history', '--config', str(self.config),
            '--access', str(self.owner_path), '--require-service'], input=json.dumps(args['input']).encode(),
            capture_output=True, timeout=30)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(len(json.loads(cli.stdout)['result']['records']), 1)
        client.call(self.owner, 'cli', {'command': 'change', 'input':
            asdict(Change('hide-service-profile', self.evidence, 1, 'suppress', control, '合成'))})
        with self.assertRaisesRegex(MemoryFailure, 'source_unavailable'):
            client.call(self.ordinary, 'cli', args)

    def test_concurrent_service_persona_updates_allow_only_one_matching_revision(self) -> None:
        client = self.start(activate=True)
        barrier = threading.Barrier(2)
        def change(number: int) -> str:
            value = {'request_id': f'concurrent-{number}', 'block': 'tone', 'text': f'合成语气{number}',
                     'evidence_id': self.evidence, 'reason': '合成确认', 'expected_revision': 0}
            barrier.wait(timeout=10)
            try:
                result = client.call(self.owner, 'cli', {'command': 'persona-set', 'input': value})
                return 'saved-' + str(result['revision'])
            except MemoryFailure as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (1, 2)))
        self.assertCountEqual(results, ['saved-1', 'persona_revision_conflict'])

    def test_trusted_service_persona_preview_confirm_and_source_are_atomic(self) -> None:
        client = self.start(activate=True)
        operation = {'kind': 'persona_set', 'block': 'tone', 'text': '合成服务确认的人设'}
        def manage(payload: dict[str, object]) -> dict[str, object]:
            return _wire_object(client.call(self.owner, 'cli', {'command': 'manage', 'input': payload}))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', {'command': 'manage', 'input': {'action': 'preview', 'operation': operation}})
        preview = manage({'action': 'preview', 'operation': operation})
        proposal_id = preview['proposal_id']
        assert isinstance(proposal_id, str)
        outcome = manage({'action': 'confirm', 'proposal_id': proposal_id, 'operation': operation})
        self.assertEqual(outcome['revision'], 1)
        self.assertEqual(manage({'action': 'confirm', 'proposal_id': proposal_id, 'operation': operation}), outcome)
        confirmation_id = outcome['confirmation_id']
        assert isinstance(confirmation_id, str)
        source = _wire_object(client.call(self.owner, 'read', {'message_id': confirmation_id}))
        event = _wire_object(source['event'])
        self.assertEqual(event['role'], 'user')
        self.assertEqual(event['text'], operation['text'])
        persona = manage({'action': 'read', 'view': 'persona'})
        self.assertEqual(_wire_rows(persona['items'])[0]['text'], operation['text'])


if __name__ == '__main__':
    unittest.main()
