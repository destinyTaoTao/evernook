from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, Callable
from unittest.mock import patch

from evernook import Change, Event, MemoryRuntime, Source, initialize, pair_client
from evernook.models import MemoryFailure
from evernook.reflection import ReflectionEngine
from evernook.reflection_model import from_file
from evernook.reflection_remote import RemoteReflectionModel
from evernook.semantic import SemanticRecall
from evernook.service import ServiceClient


class RemoteReflectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-remote-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config, self.access = initialize(self.root / 'runtime', client_id='owner', entry='owner')
        self.runtime = MemoryRuntime(config)
        self.requests: list[dict[str, Any]] = []
        self.response_kind = 'answer'
        self.delay = 0.0
        self.on_response: Callable[[], None] | None = None
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append({'body': value, 'authorization': self.headers.get('Authorization'), 'path': self.path})
                if outer.on_response:
                    outer.on_response()
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.response_kind == 'redirect':
                    self.send_response(307); self.send_header('Location', '/forbidden'); self.end_headers(); return
                actual = json.loads(value['messages'][-1]['content'])
                support = {ref: source['quote'] for ref, source in actual['evidence'].items()}
                action: dict[str, Any] = {'action': 'answer', 'value': '苍鹭' if any('苍鹭' in quote for quote in support.values()) else '银色盒子', 'support': support}
                if outer.response_kind == 'write':
                    action = {'action': 'shell', 'command': 'synthetic-only'}
                if outer.response_kind in {'search', 'unrelated_search'}:
                    action = {'action': 'search', 'query': 'ORCHID_CANARY' if outer.response_kind == 'unrelated_search' else '钥匙'}
                if outer.response_kind == 'forged':
                    action['support'] = {'invented': 'synthetic'}
                response: dict[str, Any] = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                    'content': json.dumps(action, ensure_ascii=False)}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
                if outer.response_kind == 'missing_usage':
                    del response['usage']
                if outer.response_kind == 'over_tokens':
                    response['usage']['completion_tokens'] = 513
                if outer.response_kind == 'length':
                    response['choices'][0]['finish_reason'] = 'length'
                if outer.response_kind == 'echo_key':
                    response['choices'][0]['message']['content'] = json.dumps({**action,
                        'value': os.environ['EVERNOOK_SYNTHETIC_API_KEY']})
                if outer.response_kind == 'echo_escaped_key':
                    response['choices'][0]['message']['content'] = json.dumps({**action, 'value': 'synthetic-api-secret-key'}).replace(
                        'synthetic-api-secret-key', '\\u0073ynthetic-api-secret-key')
                if outer.response_kind == 'deep_inner':
                    response['choices'][0]['message']['content'] = '{"extra":' + '[' * 1500 + '0' + ']' * 1500 + '}'
                encoded = json.dumps(response, ensure_ascii=False).encode()
                if outer.response_kind == 'deep_outer':
                    encoded = b'{"extra":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}'
                try:
                    self.send_response(200); self.send_header('Content-Length', str(len(encoded))); self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args: Any) -> None:
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.remote: dict[str, Any] = {'base_url': 'http://127.0.0.1:' + str(self.server.server_port) + '/v1',
            'model': 'synthetic-model', 'api_key_env': 'EVERNOOK_SYNTHETIC_API_KEY', 'allowed_clients': ['owner'],
            'allow_loopback_http': True, 'output_limit': 'max_completion_tokens',
            'pricing': {'input_micro_usd_per_million': 1000000, 'output_micro_usd_per_million': 2000000,
                'request_micro_usd': 0}, 'max_cost_micro_usd': 30000}
        environment = patch.dict(os.environ, {'EVERNOOK_SYNTHETIC_API_KEY': 'synthetic-api-secret-key'})
        environment.start(); self.addCleanup(environment.stop)
        self.message = self.runtime.submit(self.access, Event(Source('owner', 'history', 'main', '1'), 'user', 1,
            None, 'complete', '合成：钥匙放在银色盒子里。')).message_id

    def engine(self, **config: Any) -> ReflectionEngine:
        model = RemoteReflectionModel({**self.remote, **config})
        engine = ReflectionEngine(self.runtime, SemanticRecall(self.runtime), model)
        engine.configure(self.access)
        return engine

    def test_explicit_remote_uses_same_evidence_and_contract_without_key_in_body(self) -> None:
        engine = self.engine()
        result = engine.reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['assessment']['decision'], 'answer')
        self.assertEqual(result['conclusions'][0]['text'], '银色盒子')
        self.assertEqual(result['provider'], 'remote')
        self.assertGreater(result['usage']['cost_reserved_micro_usd'], result['usage']['cost_reported_micro_usd'])
        request = self.requests[0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['authorization'], 'Bearer synthetic-api-secret-key')
        self.assertFalse(request['body']['store']); self.assertFalse(request['body']['stream'])
        self.assertEqual(request['body']['max_completion_tokens'], 512)
        self.assertNotIn('tools', request['body'])
        self.assertNotIn('synthetic-api-secret-key', json.dumps([request['body'], result, engine.configure(self.access)]))

    def test_changed_provider_at_dispatch_prevents_http_egress(self) -> None:
        engine = self.engine()
        def changed() -> None:
            raise MemoryFailure('reflection_config_changed')
        result = engine.reflect(self.access, '钥匙', allow_remote=True, provider_check=changed)
        self.assertIn('reflection_config_changed', result['degraded'])
        self.assertEqual(self.requests, [])

    def test_remote_transcript_evidence_keeps_unverified_marker_without_model_identity(self) -> None:
        from evernook import Transcription
        from evernook.transcription import submit
        material = Transcription(Source('owner', 'voice', 'main', 'asr-remote'), 1, None, 'complete',
            '合成语音问题：钥匙放在银色盒子里。', 'synthetic-asr-provider', 'synthetic-asr-model', 'zh-CN',
            'local', 100, 900, 0.7, speaker='user')
        submit(self.runtime, None, self.access, material)
        self.engine().reflect(self.access, '合成语音问题', allow_remote=True)
        packet = json.loads(self.requests[-1]['body']['messages'][-1]['content'])
        marked = [item['transcription'] for item in packet['evidence'].values() if 'transcription' in item]
        self.assertTrue(marked)
        self.assertEqual(marked[0]['confirmation'], 'unverified')
        self.assertEqual(marked[0]['speaker'], 'user')
        self.assertNotIn('synthetic-asr-provider', json.dumps(packet))
        self.assertNotIn('synthetic-asr-model', json.dumps(packet))

    def test_no_implicit_remote_client_or_sensitive_input_egress(self) -> None:
        engine = self.engine()
        with self.assertRaisesRegex(MemoryFailure, 'remote_reflection_not_permitted'):
            engine.reflect(self.access, '钥匙')
        reader = pair_client(self.runtime.root / 'runtime.json', client_id='reader', entry='reader',
            access_path=self.runtime.root / 'mcp-access-reader.json', scopes=('recall',))
        with self.assertRaisesRegex(MemoryFailure, 'remote_reflection_not_permitted'):
            engine.reflect(reader, '钥匙', allow_remote=True)
        result = engine.reflect(self.access, 'password=synthetic-only-39', allow_remote=True)
        self.assertEqual(result['state'], 'protected_route_required')
        private = self.runtime.submit(self.access, Event(Source('owner', 'private', 'main', '2'), 'user', 2,
            None, 'complete', '合成私密小秘密', sensitive=True))
        with self.assertRaisesRegex(MemoryFailure, 'remote_reflection_private_input'):
            engine.reflect(self.access, '合成私密小秘密', current_input_id=private.message_id, allow_remote=True)
        self.assertEqual(self.requests, [])

    def test_private_and_protected_sources_never_enter_request(self) -> None:
        for index, values in enumerate(({'sensitive': True}, {'protected_source': True}), 2):
            self.runtime.submit(self.access, Event(Source('owner', 'history', 'main', str(index)), 'user', index,
                None, 'complete', '钥匙 SYNTHETIC-PRIVATE-DO-NOT-SEND', **values))
        self.engine().reflect(self.access, '钥匙', allow_remote=True)
        self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(self.requests))

    def test_cost_and_input_prechecks_prevent_network_calls(self) -> None:
        result = self.engine(max_cost_micro_usd=1).reflect(self.access, '钥匙', allow_remote=True)
        self.assertIn('reflection_cost_budget', result['degraded'])
        result = self.engine().reflect(self.access, '钥匙' * 2000, allow_remote=True)
        self.assertIn('reflection_input_budget', result['degraded'])
        self.assertEqual(self.requests, [])

    def test_daily_budget_is_shared_and_survives_new_engine(self) -> None:
        first = self.engine(daily_cost_micro_usd=5120)
        second = self.engine(daily_cost_micro_usd=5120)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda engine: engine.reflect(self.access, '钥匙', allow_remote=True), (first, second)))
        self.assertEqual(sum(result['state'] == 'complete' for result in results), 1, results)
        self.assertEqual(sum('reflection_daily_budget' in result['degraded'] for result in results), 1, results)
        self.assertEqual(len(self.requests), 1)
        restarted = self.engine(daily_cost_micro_usd=5120)
        result = restarted.reflect(self.access, '钥匙', allow_remote=True)
        self.assertIn('reflection_daily_budget', result['degraded'])
        self.assertEqual(len(self.requests), 1)

    def test_daily_budget_keeps_failed_reservation_and_requires_configuration(self) -> None:
        self.engine()  # Configure the reflection clock without enabling the daily ledger.
        guarded = ReflectionEngine(self.runtime, SemanticRecall(self.runtime),
            RemoteReflectionModel({**self.remote, 'daily_cost_micro_usd': 5120}))
        missing = guarded.reflect(self.access, '钥匙', allow_remote=True)
        self.assertIn('reflection_daily_budget_not_configured', missing['degraded'])
        self.assertEqual(self.requests, [])
        guarded.configure(self.access)
        self.response_kind = 'missing_usage'
        failed = guarded.reflect(self.access, '钥匙', allow_remote=True)
        self.assertIn('reflection_remote_usage_unavailable', failed['degraded'])
        self.assertEqual(failed['usage']['daily_cost_reserved_micro_usd'], 5120)
        self.response_kind = 'answer'
        blocked = guarded.reflect(self.access, '钥匙', allow_remote=True)
        self.assertIn('reflection_daily_budget', blocked['degraded'])
        self.assertEqual(len(self.requests), 1)

    def test_daily_status_reads_consistent_snapshot_during_reservations(self) -> None:
        engine = self.engine(daily_cost_micro_usd=100)
        def reserve() -> None:
            for _ in range(20):
                engine._reserve_daily(1, 100, lambda: 10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(reserve)
            for _ in range(20):
                status = engine.daily_budget_status(self.access)
                self.assertEqual(status['state'], 'ready')
                self.assertEqual(status['reserved_micro_usd'] + status['remaining_micro_usd'], status['limit_micro_usd'])
            writer.result()
        self.assertEqual(engine.daily_budget_status(self.access)['reserved_micro_usd'], 20)

    def test_daily_budget_rollover_and_backwards_clock_fail_closed(self) -> None:
        engine = self.engine(daily_cost_micro_usd=100)
        with patch('evernook.reflection.datetime') as clock:
            clock.now.return_value = datetime(2030, 1, 2, tzinfo=timezone.utc)
            self.assertEqual(engine._reserve_daily(100, 100, lambda: 10), ('2030-01-02', 100))
            self.assertEqual(engine.daily_budget_status(self.access)['state'], 'exhausted')
            clock.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(engine.daily_budget_status(self.access)['state'], 'clock_rollback')
            with self.assertRaisesRegex(MemoryFailure, 'reflection_daily_clock_rollback'):
                engine._reserve_daily(1, 100, lambda: 10)
            clock.now.return_value = datetime(2030, 1, 3, tzinfo=timezone.utc)
            status = engine.daily_budget_status(self.access)
            self.assertEqual((status['state'], status['reserved_micro_usd']), ('ready', 0))
            self.assertEqual(engine._reserve_daily(40, 100, lambda: 10), ('2030-01-03', 40))
        limited = pair_client(self.runtime.root / 'runtime.json', client_id='limited-budget', entry='limited',
            access_path=self.runtime.root / 'mcp-access-limited-budget.json', scopes=('capture', 'recall'))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            engine.daily_budget_status(limited)
        with self.runtime._connection() as db:
            db.execute("UPDATE reflection_daily_cost_v1 SET utc_day='not-a-date' WHERE id=1")
        self.assertEqual(engine.daily_budget_status(self.access), {'state': 'unavailable'})
        with self.assertRaisesRegex(MemoryFailure, 'reflection_daily_budget_unavailable'):
            engine._reserve_daily(1, 100, lambda: 10)
        for value in (0, -1, True, 1.5, 1000000001):
            with self.subTest(value=value), self.assertRaisesRegex(MemoryFailure, 'invalid_reflection_config'):
                RemoteReflectionModel({**self.remote, 'daily_cost_micro_usd': value})

    def test_daily_budget_survives_independent_service_restart(self) -> None:
        profile = self.root / 'daily-remote.json'
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'openai-compatible',
            'remote': {**self.remote, 'daily_cost_micro_usd': 5120}, 'policy': {}}), encoding='utf-8')
        config = self.runtime.root / 'runtime.json'
        access_path = self.runtime.root / 'mcp-access-owner.json'
        access_path.write_text(json.dumps(asdict(self.access)), encoding='utf-8')
        endpoint = config.parent / 'memory-service.json'
        processes: list[subprocess.Popen[bytes]] = []
        def start(activate: bool) -> ServiceClient:
            previous = endpoint.read_bytes() if endpoint.exists() else None
            command = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                       '--reflection-config', str(profile)]
            if activate:
                command += ['--activate-owner', str(access_path)]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(process)
            deadline = time.monotonic() + 15
            while not endpoint.exists() or endpoint.read_bytes() == previous:
                self.assertIsNone(process.poll())
                if time.monotonic() >= deadline:
                    self.fail('service startup timeout')
                time.sleep(.05)
            return ServiceClient(config)
        try:
            client = start(True)
            status_request = {'command': 'manage', 'input': {'action': 'read', 'view': 'status'}}
            status = client.call(self.access, 'cli', status_request)['reflection_daily_budget']
            self.assertEqual(status['state'], 'setup_required')
            self.assertEqual(status['limit_micro_usd'], 5120)
            self.assertNotIn('synthetic-api-secret-key', json.dumps(status))
            self.assertNotIn('127.0.0.1', json.dumps(status))
            client.call(self.access, 'cli', {'command': 'reflect-configure', 'input': {}})
            status = client.call(self.access, 'cli', status_request)['reflection_daily_budget']
            self.assertEqual((status['state'], status['reserved_micro_usd'], status['remaining_micro_usd']), ('ready', 0, 5120))
            self.delay = 3
            errors: list[str] = []
            def call() -> None:
                try:
                    client.reflect(self.access, '钥匙', allow_remote=True)
                except MemoryFailure as error:
                    errors.append(error.code)
            caller = threading.Thread(target=call)
            caller.start()
            deadline = time.monotonic() + 10
            while not self.requests:
                if not caller.is_alive() or time.monotonic() >= deadline:
                    self.fail('loopback provider was not reached')
                time.sleep(.05)
            processes[-1].terminate(); processes[-1].wait(15)
            caller.join(10)
            self.assertFalse(caller.is_alive())
            self.assertTrue(errors)
            self.delay = 0
            client = start(False)
            status = client.call(self.access, 'cli', status_request)['reflection_daily_budget']
            self.assertEqual((status['state'], status['reserved_micro_usd'], status['remaining_micro_usd']), ('exhausted', 5120, 0))
            result = client.reflect(self.access, '钥匙', allow_remote=True)
            self.assertIn('reflection_daily_budget', result['degraded'])
            self.assertEqual(len(self.requests), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(15)
    def test_bad_or_dangerous_responses_never_become_inferences_or_retry_http(self) -> None:
        for kind in ('write', 'forged', 'missing_usage', 'over_tokens', 'length', 'echo_key', 'echo_escaped_key', 'redirect', 'deep_inner', 'deep_outer'):
            with self.subTest(kind=kind):
                self.response_kind = kind; before = len(self.requests)
                result = self.engine().reflect(self.access, '钥匙', allow_remote=True)
                self.assertEqual(result['conclusions'], [])
                self.assertIsNone(result['assessment'])
                self.assertEqual(len(self.requests) - before, 1)
                self.assertNotIn('synthetic-api-secret-key', json.dumps(result))
                if kind == 'deep_inner':
                    self.assertEqual(result['usage']['input_tokens'], 100)
                    self.assertFalse(result['usage']['metering_incomplete'])

    def test_cancel_timeout_and_failed_request_reserve_are_retained(self) -> None:
        self.delay = 3
        processes: list[subprocess.Popen[bytes]] = []
        class Observed(subprocess.Popen[bytes]):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, **kwargs)
                processes.append(self)
        with patch('evernook.reflection_remote.subprocess.Popen', Observed):
            result = self.engine().reflect(self.access, '钥匙', allow_remote=True, timeout=.7)
        self.assertEqual(result['state'], 'partial')
        self.assertGreater(result['usage']['cost_reserved_micro_usd'], 0)
        self.assertTrue(result['usage']['metering_incomplete'])
        self.assertEqual(result['conclusions'], [])
        cancel = threading.Event(); timer = threading.Timer(.5, cancel.set); timer.start()
        try:
            with patch('evernook.reflection_remote.subprocess.Popen', Observed):
                result = self.engine().reflect(self.access, '钥匙', allow_remote=True, cancel=cancel)
        finally:
            timer.cancel()
        self.assertIn('reflection_canceled', result['degraded'])
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_config_requires_secure_explicit_provider_and_supports_schema1(self) -> None:
        profile = self.root / 'reflection.json'
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'openai-compatible', 'remote': self.remote, 'policy': {}}), encoding='utf-8')
        self.assertIsInstance(from_file(self.runtime, SemanticRecall(self.runtime), profile).model, RemoteReflectionModel)
        for change in ({'allow_loopback_http': False}, {'base_url': 'https://user:secret@example.invalid/v1'},
                {'base_url': 'https://example.invalid/v1?key=secret'}, {'api_key': 'plaintext'}):
            with self.subTest(change=change), self.assertRaises(MemoryFailure):
                RemoteReflectionModel({**self.remote, **change})

    def test_opaque_credentials_are_checked_before_serialization_and_after_inner_decode(self) -> None:
        for key in ('synthetic"opaque-key', 'synthetic\\opaque-key'):
            with self.subTest(key_kind='opaque'), patch.dict(os.environ, {'EVERNOOK_SYNTHETIC_API_KEY': key}):
                engine = self.engine()
                before = len(self.requests)
                result = engine.reflect(self.access, '钥匙以及' + key, allow_remote=True)
                self.assertEqual(result['conclusions'], [])
                self.assertEqual(len(self.requests), before)
                self.response_kind = 'echo_key'
                result = engine.reflect(self.access, '钥匙', allow_remote=True)
                self.assertEqual(result['conclusions'], [])
                self.assertNotIn(key, str(result))
                self.assertIn('reflection_remote_invalid_output', result['degraded'])

    def test_failed_scope_reservations_are_not_refunded_and_caps_are_configurable(self) -> None:
        self.response_kind = 'search'
        result = self.engine(max_cost_micro_usd=6000).reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('reflection_remote_evidence_scope', result['degraded'])
        self.assertEqual(result['usage']['cost_reserved_micro_usd'], 5120)
        self.response_kind = 'answer'
        result = self.engine(output_tokens=128).reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.requests[-1]['body']['max_completion_tokens'], 128)
        tiny = self.engine(pricing={'input_micro_usd_per_million': 1, 'output_micro_usd_per_million': 1, 'request_micro_usd': 0})
        self.assertIsInstance(tiny.model, RemoteReflectionModel)
        assert isinstance(tiny.model, RemoteReflectionModel)
        self.assertEqual(tiny.model.reserve_cost(1, 1), 2)

    def test_remote_cannot_expand_egress_to_an_unrelated_ordinary_topic(self) -> None:
        self.runtime.submit(self.access, Event(Source('owner', 'unrelated', 'main', '2'), 'user', 2,
            '1999-01-01T00:00:00Z', 'complete', 'Synthetic ORCHID_CANARY project code is lapwing.'))
        self.response_kind = 'unrelated_search'
        result = self.engine().reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('reflection_remote_evidence_scope', result['degraded'])
        self.assertEqual(result['usage']['search_rounds'], 1)
        self.assertEqual(result['conclusions'], [])
        self.assertNotIn('ORCHID_CANARY', json.dumps(self.requests))
        actual = json.loads(self.requests[0]['body']['messages'][-1]['content'])
        self.assertEqual(actual['allowed_actions'], ['answer', 'abstain', 'clarify', 'refuse'])

    def test_source_withdrawal_during_http_discards_every_claim_and_proof(self) -> None:
        confirmation = self.runtime.submit(self.access, Event(Source('owner', 'control', 'main', '2'), 'user', 2,
            None, 'complete', '合成：隐藏旧线索。')).message_id
        engine = self.engine()
        def withdraw() -> None:
            self.runtime.change(self.access, Change('remote-hide', self.message, 1, 'suppress', confirmation, 'synthetic'))
        self.on_response = withdraw
        result = engine.reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(result['conclusions'], [])
        self.assertIsNone(result['assessment'])
        self.assertEqual(result['evidence'], [])
        self.assertIn('memory_changed_during_reflection', result['degraded'])

    def test_real_protected_inbox_is_not_a_remote_source(self) -> None:
        from evernook.protected import initialize_protected_inbox
        from evernook import Transcription
        from evernook.transcription import submit
        inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys', b'synthetic vault password 123')
        inbox.submit(self.access, Event(Source('owner', 'vault', 'main', '2'), 'user', 2, None, 'complete',
            '钥匙 SYNTHETIC-VAULT-ONLY-DO-NOT-SEND'))
        submit(self.runtime, inbox, self.access, Transcription(Source('owner', 'vault', 'voice', '3'), 3, None, 'complete',
            '钥匙 SYNTHETIC-ASR-VAULT-ONLY', 'synthetic', 'asr-v1', 'zh-CN', 'local', 0, 100, sensitive=True))
        result = self.engine().reflect(self.access, '钥匙', allow_remote=True)
        self.assertEqual(result['state'], 'complete')
        self.assertNotIn('SYNTHETIC-VAULT', json.dumps(self.requests))
        self.assertNotIn('SYNTHETIC-ASR-VAULT', json.dumps(self.requests))

    def test_local_profiles_keep_v1_compatibility_and_never_select_cloud(self) -> None:
        local = {'profile': 'qwen3-0.6b-experimental-reflection-v1', 'node': sys.executable,
            'transformers_dir': str(self.root), 'model_dir': str(self.root)}
        profile = self.root / 'local.json'
        profile.write_text(json.dumps({'schema_version': 1, **local, 'policy': {}}), encoding='utf-8')
        original = from_file(self.runtime, SemanticRecall(self.runtime), profile)
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'local', 'local': local, 'policy': {}}), encoding='utf-8')
        updated = from_file(self.runtime, SemanticRecall(self.runtime), profile)
        self.assertEqual(original.identity, updated.identity)
        self.assertNotIsInstance(updated.model, RemoteReflectionModel)
        self.assertEqual(self.requests, [])

    def test_actual_service_python_sdk_cli_mcp_and_typescript_sdk_share_provider(self) -> None:
        profile = self.root / 'remote.json'
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'openai-compatible', 'remote': self.remote, 'policy': {}}), encoding='utf-8')
        config = self.runtime.root / 'runtime.json'
        access_path = self.runtime.root / 'mcp-access-owner.json'
        access_path.write_text(json.dumps(asdict(self.access)), encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
            '--reflection-config', str(profile), '--activate-owner', str(access_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + 15
            while not (config.parent / 'memory-service.json').exists():
                self.assertIsNone(process.poll())
                if time.monotonic() >= end:
                    self.fail('service startup timeout')
                time.sleep(.05)
            client = ServiceClient(config)
            configured = client.call(self.access, 'cli', {'command': 'reflect-configure', 'input': {}})
            self.assertEqual(configured['provider'], 'remote')
            with self.assertRaisesRegex(MemoryFailure, 'remote_reflection_not_permitted'):
                client.reflect(self.access, '钥匙')
            self.assertEqual(client.reflect(self.access, '钥匙', allow_remote=True)['state'], 'complete')
            environment = {k: v for k, v in os.environ.items() if k != 'EVERNOOK_SYNTHETIC_API_KEY'}
            command = [sys.executable, '-B', '-m', 'evernook.cli', 'reflect', '--config', str(config),
                '--access', str(access_path), '--require-service']
            cli = subprocess.run(command, input=json.dumps({'query': '钥匙', 'allow_remote': True}).encode(),
                env=environment, capture_output=True, check=True, timeout=20)
            self.assertEqual(json.loads(cli.stdout)['result']['provider'], 'remote')
            async def mcp_check() -> None:
                from mcp import Client, StdioServerParameters
                before = len(self.requests)
                async with Client(StdioServerParameters(command=sys.executable, args=['-B', '-m', 'evernook.mcp_server',
                        '--config', str(config), '--access', str(access_path), '--require-service'], env=environment), read_timeout_seconds=20) as mcp:
                    denied = await mcp.call_tool('evernook_v1_reflect', {'query': '钥匙', 'allow_remote': True})
                    self.assertTrue(denied.is_error)
                self.assertEqual(len(self.requests), before)
                async with Client(StdioServerParameters(command=sys.executable, args=['-B', '-m', 'evernook.mcp_server',
                        '--config', str(config), '--access', str(access_path), '--require-service', '--allow-remote-reflection'], env=environment), read_timeout_seconds=20) as mcp:
                    response = await mcp.call_tool('evernook_v1_reflect', {'query': '钥匙', 'allow_remote': True})
                    self.assertFalse(response.is_error)
                    self.assertEqual(response.structured_content['result']['provider'], 'remote')
            asyncio.run(mcp_check())
            sdk_module = os.environ.get('EVERNOOK_TEST_SDK_MODULE', str(Path('plugins/harnesses/src/client.ts').resolve()))
            script = "import {pathToFileURL} from 'node:url'; const {MemoryClient}=await import(pathToFileURL(process.argv[1]).href); const c=new MemoryClient(JSON.parse(process.argv[2])); await c.connect(); try { console.log(JSON.stringify(await c.reflect({query:'钥匙',allow_remote:true}))); } finally {await c.close();}"
            node = subprocess.run(['C:/Program Files/nodejs/node.exe', '--input-type=module', '-e', script, sdk_module,
                json.dumps({'python': sys.executable, 'config': str(config), 'access': str(access_path), 'entry': 'owner',
                    'serviceRequired': True, 'allowRemoteReflection': True, 'cwd': str(Path.cwd())})], env=environment, capture_output=True, check=True, timeout=25)
            self.assertEqual(json.loads(node.stdout)['provider'], 'remote')
            self.assertEqual(len(self.requests), 4)
        finally:
            process.terminate(); process.wait(15)
