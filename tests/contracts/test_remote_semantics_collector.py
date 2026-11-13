from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from remote_semantics import collect, safe_response, SUITE_HASH


class SafeResponseTests(unittest.TestCase):
    def wrap(self, value: object) -> bytes:
        return json.dumps({'status': 'ok', 'body': json.dumps(value)}).encode()

    def test_retains_response_and_model(self) -> None:
        value = {'model': 'model-v1', 'choices': [{'message': {'content': 'synthetic'}}]}
        self.assertEqual(json.loads(safe_response(self.wrap(value), 'secret-key') or ''), value)

    def test_rejects_key_in_nested_escaped_json(self) -> None:
        inner = '{"value":"\\u0073ecret-key"}'
        self.assertIsNone(safe_response(self.wrap({'choices': [{'message': {'content': inner}}]}), 'secret-key'))

    def test_rejects_malformed_and_failed_envelopes(self) -> None:
        self.assertIsNone(safe_response(b'not-json', 'secret-key'))
        self.assertIsNone(safe_response(b'{"status":"http_error"}', 'secret-key'))
        self.assertIsNone(safe_response(b'{"status":"ok","body":"{"}', 'secret-key'))

    def test_real_worker_retains_response_and_budget_blocks_dispatch(self) -> None:
        calls: list[dict[str, object]] = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'model': 'synthetic-v1', 'choices': [{'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(
                        {'action': 'answer', 'value': 'x', 'support': {'e1': 'x'}})}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args: object) -> None:
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        case = {'id': 'one', 'category': 'synthetic', 'question': 'q',
                'sources': [{'id': 'a', 'role': 'user', 'text': 'x'}],
                'expected': {'decision': 'answer', 'value': 'x', 'source_ids': ['a']}}
        config = {'base_url': f'http://127.0.0.1:{server.server_port}', 'model': 'synthetic',
            'api_key_env': 'EVERNOOK_TEST_COLLECTOR_KEY', 'allowed_clients': ['synthetic'],
            'allow_loopback_http': True, 'output_limit': 'max_tokens',
            'pricing': {'input_micro_usd_per_million': 1000000, 'output_micro_usd_per_million': 1000000,
                        'request_micro_usd': 0}, 'max_cost_micro_usd': 5000}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EVERNOOK_TEST_COLLECTOR_KEY': 'synthetic-secret-key'}), \
                patch('remote_semantics.load_cases', return_value=([case], SUITE_HASH)):
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'screen_cost_budget'):
                collect(root / 'blocked', config, 1)
            with self.assertRaisesRegex(ValueError, 'screen_output_budget'):
                collect(root / 'too-large', config, 100000, output_tokens=1024)
            with self.assertRaisesRegex(ValueError, 'invalid_diagnostic_thinking'):
                collect(root / 'bad-mode', config, 5000, diagnostic_thinking='other')
            self.assertEqual(calls, [])
            report = collect(root / 'run', config, 5000)
            raw = (root / 'run' / 'one.response.json').read_bytes()
            self.assertNotIn(b'synthetic-secret-key', raw)
            self.assertEqual(report['rows'][0]['response_model'], 'synthetic-v1')
            self.assertEqual(report['rows'][0]['judgment']['state'], 'passed')
            self.assertEqual(len(calls), 1)
            expanded = {**config, 'output_tokens': 1024, 'max_cost_micro_usd': 6000}
            result = collect(root / 'expanded', expanded, 6000, output_tokens=1024)
            self.assertEqual(calls[-1]['max_tokens'], 1024)
            self.assertEqual(result['rows'][0]['reserved_micro_usd'], 5120)
            self.assertEqual(result['budget']['output_tokens'], 1024)
            diagnostic = collect(root / 'disabled', expanded, 6000, output_tokens=1024,
                                 diagnostic_thinking='disabled')
            self.assertEqual(calls[-1]['thinking'], {'type': 'disabled'})
            self.assertEqual(calls[-1]['max_tokens'], 1024)
            self.assertEqual(diagnostic['diagnostic_thinking'], 'disabled')
            self.assertEqual(len(diagnostic['rows'][0]['request_body_sha256']), 64)
            self.assertEqual(diagnostic['rows'][0]['reserved_micro_usd'], 5120)
            low = collect(root / 'low', expanded, 6000, output_tokens=1024,
                          diagnostic_thinking='low')
            self.assertEqual(calls[-1]['reasoning_effort'], 'low')
            self.assertNotIn('thinking', calls[-1])
            self.assertEqual(calls[-1]['max_tokens'], 1024)
            self.assertEqual(low['diagnostic_thinking'], 'low')
            self.assertEqual(low['rows'][0]['reserved_micro_usd'], 5120)


if __name__ == '__main__':
    unittest.main()
