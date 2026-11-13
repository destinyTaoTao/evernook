from __future__ import annotations

import json
import unittest
from typing import Any

from evernook.models import MemoryFailure
from evernook.reflection_protocol import build_messages, translate


class ReflectionProtocolTests(unittest.TestCase):
    def payload(self) -> dict[str, Any]:
        return {'question': '合成：钥匙在哪里？', 'evidence': [{'evidence_id': 'e7', 'message_id': 'actual-source',
            'role': 'user', 'quote': '合成：钥匙放在银色盒子里。', 'occurred_at': '2020-01-01T00:00:00Z',
            'learned_at': '2026-01-01T00:00:00Z'}], 'allowed_view_ids': [], 'coverage': [{'runtime_detail': 'not_model_input'}]}

    def test_messages_distinguish_real_evidence_from_demonstrations_and_statement_time(self) -> None:
        messages = build_messages(self.payload())
        self.assertNotIn('有证据的推断', json.dumps(messages, ensure_ascii=False))
        actual = json.loads(messages[-1]['content'])
        self.assertEqual(set(actual['evidence']), {'e7'})
        self.assertEqual(actual['evidence']['e7']['statement_at'], '2020-01-01T00:00:00Z')
        self.assertNotIn('learned_at', messages[-1]['content'])
        self.assertNotIn('runtime_detail', messages[-1]['content'])
        self.assertLess(messages[2]['content'].index('"value"'), messages[2]['content'].index('"support"'))
        empty = build_messages({'question': '合成：谁？', 'evidence': []})
        self.assertEqual(json.loads(empty[-1]['content'])['evidence'], {})

    def test_answer_translates_only_with_exact_current_support(self) -> None:
        action = translate({'action': 'answer', 'value': '银色盒子', 'support': {'e7': '钥匙放在银色盒子里。'}}, self.payload())
        self.assertEqual(action['conclusions'][0]['text'], '银色盒子')
        self.assertEqual(action['conclusions'][0]['support'], [{'evidence_id': 'e7', 'quote': '钥匙放在银色盒子里。'}])
        self.assertEqual(action['assessment']['decision'], 'answer')

    def test_metadata_year_cannot_be_presented_as_an_explicit_event_year(self) -> None:
        payload = {'evidence': [{'evidence_id': 'e1', 'message_id': 'source', 'role': 'user',
            'quote': '合成：杯子摔破了，但不记得哪一年。', 'occurred_at': '2024-01-01T00:00:00Z'}]}
        action = {'action': 'answer', 'value': '2024年', 'support': {'e1': '杯子摔破了，但不记得哪一年。'}}
        result = translate(action, payload)
        self.assertEqual(result['conclusions'], [])
        self.assertEqual(result['assessment']['decision'], 'abstain')
        self.assertEqual(result['assessment']['origin'], 'explicit_year_guard')
        for value in ('2033年', '1980-02-01', '1789'):
            with self.subTest(value=value):
                self.assertEqual(translate({**action, 'value': value}, payload)['conclusions'], [])
        payload['evidence'][0]['quote'] = '合成：2018年杯子摔破了。'
        result = translate({'action': 'answer', 'value': '2018年', 'support': {'e1': '2018年杯子摔破了。'}}, payload)
        self.assertEqual(result['conclusions'][0]['text'], '2018年')

    def test_missing_or_forged_or_demonstration_support_is_rejected(self) -> None:
        for support in ({}, {'e1': '不存在'}, {'demo-a': '钥匙收在蓝色包里。'}, {'e7': '钥匙放在金色盒子里。'}, {'e7': {'quote': '嵌套错误'}}):
            with self.subTest(support=support), self.assertRaisesRegex(MemoryFailure, 'reflection_invalid_support'):
                translate({'action': 'answer', 'value': '金色盒子', 'support': support}, self.payload())
        with self.assertRaises(MemoryFailure):
            translate({'action': 'shell', 'command': 'synthetic'}, self.payload())

    def test_abstention_and_refusal_keep_real_reason_support_without_claims(self) -> None:
        for decision in ('abstain', 'refuse'):
            action = translate({'action': decision, 'support': {'e7': '合成：钥匙放在银色盒子里。'}}, self.payload())
            self.assertEqual(action['conclusions'], [])
            self.assertEqual(action['assessment']['decision'], decision)
            self.assertEqual(action['assessment']['support'][0]['evidence_id'], 'e7')
        self.assertEqual(translate({'action': 'abstain', 'support': {}}, {'evidence': []})['conclusions'], [])

    def test_finite_user_preference_conflict_blocks_one_sided_answer_but_not_quotes_or_temporary_scope(self) -> None:
        payload = {'evidence': [{'evidence_id': 'e1', 'role': 'user', 'quote': '我偏好简短回答。'},
            {'evidence_id': 'e2', 'role': 'user', 'quote': '我偏好详细回答。'}]}
        result = translate({'action': 'answer', 'value': '详细回答', 'support': {'e2': '我偏好详细回答。'}}, payload)
        self.assertEqual(result['conclusions'], [])
        self.assertEqual(set(result['conflicts'][0]), {'e1', 'e2'})
        self.assertEqual(result['assessment']['origin'], 'finite_preference_guard')
        payload['evidence'][0]['quote'] = '这次我偏好简短回答。'
        result = translate({'action': 'answer', 'value': '详细回答', 'support': {'e2': '我偏好详细回答。'}}, payload)
        self.assertEqual(result['conclusions'][0]['text'], '详细回答')

    def test_search_read_and_view_remain_strictly_bounded_actions(self) -> None:
        self.assertEqual(translate({'action': 'search', 'query': '盒子'}, self.payload()), {'action': 'search', 'query': '盒子'})
        with self.assertRaises(MemoryFailure):
            translate({'action': 'search', 'query': '我的密码是Synthetic-Only-39!'}, self.payload())
        with self.assertRaises(MemoryFailure):
            translate({'action': 'view', 'view_id': 'not-approved'}, self.payload())

    def test_exact_quote_is_not_a_semantic_entailment_certificate(self) -> None:
        # A small model can still misinterpret a real quote; this must remain an inference.
        action = translate({'action': 'answer', 'value': '另一个地点', 'support': {'e7': '钥匙放在银色盒子里。'}}, self.payload())
        self.assertTrue(action['conclusions'][0]['inference'])
        self.assertNotIn('verified', action['conclusions'][0])

    def test_assistant_and_quoted_preferences_are_not_independent_conflict_evidence(self) -> None:
        payload = {'evidence': [{'evidence_id': 'e1', 'role': 'assistant', 'quote': '我偏好简短回答。'},
            {'evidence_id': 'e2', 'role': 'user', 'quote': '我偏好详细回答。'}]}
        action = {'action': 'answer', 'value': '详细回答', 'support': {'e2': '我偏好详细回答。'}}
        self.assertEqual(translate(action, payload)['assessment']['origin'], 'model')
        payload['evidence'][0].update(role='user', quote='同事说：“我偏好简短回答。”')
        self.assertEqual(translate(action, payload)['assessment']['origin'], 'model')


class ReflectionAdapterTests(unittest.TestCase):
    def test_invalid_json_or_support_is_reported_for_fresh_generation_without_repair(self) -> None:
        import hashlib
        from pathlib import Path
        import sys
        import tempfile
        from unittest.mock import patch
        from evernook.reflection_model import LocalReflectionModel
        with tempfile.TemporaryDirectory(prefix='evernook-reflection-wire-') as directory:
            root = Path(directory)
            model = LocalReflectionModel(node=Path(sys.executable), transformers_dir=root, model_dir=root)
            script = root / 'synthetic-output.py'
            for raw, reason in (('{"action":"abstain","support":{}', 'invalid_json'),
                    ('{"action":"answer","value":"虚构","support":{"e99":"虚构"}}', 'invalid_support')):
                script.write_text('import sys,json\nsys.stdin.buffer.read()\nprint(json.dumps(' + repr({
                    'status': 'ok', 'action_json': raw, 'input_tokens': 123, 'output_tokens': 12, 'network_attempts': 0}) + '))\n', encoding='utf-8')
                model.worker, model.worker_digest = script, hashlib.sha256(script.read_bytes()).hexdigest()
                with patch('evernook.reflection_model._FILES', {}):
                    turn = model.turn({'question': '合成：谁？', 'evidence': []}, input_tokens=4096, output_tokens=512, timeout=5, cancel=None)
                self.assertEqual(turn.action, {'action': 'model_retry', 'reason': reason})
                self.assertEqual((turn.input_tokens, turn.output_tokens), (123, 12))

    def test_protocol_bytes_are_part_of_model_identity_and_checked_before_worker(self) -> None:
        from pathlib import Path
        import sys
        import tempfile
        from unittest.mock import patch
        from evernook.reflection_model import LocalReflectionModel
        with tempfile.TemporaryDirectory(prefix='evernook-reflection-protocol-') as directory:
            root = Path(directory)
            model = LocalReflectionModel(node=Path(sys.executable), transformers_dir=root, model_dir=root)
            original = Path.read_bytes
            def changed(path: Path) -> bytes:
                data = original(path)
                return data + b'\n# synthetic protocol edit' if path == model.protocol else data
            with patch.object(Path, 'read_bytes', changed), patch('evernook.reflection_model._FILES', {}), patch('evernook.reflection_model.subprocess.Popen') as spawn:
                other = LocalReflectionModel(node=Path(sys.executable), transformers_dir=root, model_dir=root)
                self.assertNotEqual(other.processor_version, model.processor_version)
                with self.assertRaisesRegex(MemoryFailure, 'reflection_integrity_mismatch'):
                    model.turn({'question': 'synthetic', 'evidence': []}, input_tokens=4096, output_tokens=512, timeout=5, cancel=None)
                spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
