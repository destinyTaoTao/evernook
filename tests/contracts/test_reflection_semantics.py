from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import json
from typing import Any

from probes.admission.semantic_reflection import judge, judge_raw, load_cases, packet


def wire(action: object) -> list[dict[str, Any]]:
    return [{'status': 'ok', 'network_attempts': 0, 'action_json': json.dumps(action)}]


class SemanticReflectionEvaluationTests(unittest.TestCase):
    def test_frozen_diagnostic_identity_and_model_packet_do_not_contain_gold(self) -> None:
        cases, digest = load_cases(Path('eval/zh-reflection-semantics-v1'))
        self.assertEqual(digest, 'eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0')
        self.assertEqual(len(cases), 10)
        changed = deepcopy(cases[0]); changed['expected']['value'] = 'GOLD_MUST_NOT_REACH_MODEL'
        self.assertEqual(packet(cases[0]), packet(changed))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('cases.json', 'manifest.json'):
                (root / name).write_bytes((Path('eval/zh-reflection-semantics-v1') / name).read_bytes())
            (root / 'cases.json').write_bytes((root / 'cases.json').read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                load_cases(root)

    def test_rule_credit_and_blanket_abstention_do_not_pass_as_semantic_success(self) -> None:
        expected = {'decision': 'answer', 'value': '目标', 'source_ids': ['a']}
        action: dict[str, Any] = {'action': 'done', 'conclusions': [{'text': '目标'}], 'assessment': {
            'decision': 'answer', 'origin': 'finite_preference_guard', 'support': [{'evidence_id': 'e1'}]}}
        self.assertEqual(judge(expected, action, {'e1': 'a'})['state'], 'guarded')
        action['assessment'].update(decision='abstain', origin='model')
        action['conclusions'] = []
        result = judge(expected, action, {'e1': 'a'})
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['false_abstention'])
        action['assessment']['decision'] = 'answer'; action['conclusions'] = [{'text': '目标'}]
        self.assertEqual(judge(expected, action, {'e1': 'a'})['state'], 'passed')

    def test_raw_wrong_assertion_is_visible_even_when_citation_rejection_blocks_final_action(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1'))
        case = next(row for row in cases if row['id'] == 'assistant-guess')
        payload, aliases = packet(case)
        raw = {'action': 'answer', 'value': '陶艺', 'support': {'e1': 'not an original quote'}}
        result = judge_raw(case['expected'], wire(raw), payload, aliases)
        self.assertEqual(result['reason'], 'invalid_support')
        self.assertTrue(result['unsupported_answer'])
        raw['support'] = {'e1': case['sources'][0]['text']}
        result = judge_raw(case['expected'], wire(raw), payload, aliases)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue(result['unsupported_answer'])

    def test_raw_model_credit_is_separate_from_guard_and_unobserved_output(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1'))
        case = cases[0]; payload, aliases = packet(case)
        raw = {'action': 'answer', 'value': case['expected']['value'],
            'support': {ref: source['quote'] for ref, source in zip(aliases, payload['evidence'])}}
        self.assertEqual(judge_raw(case['expected'], wire(raw), payload, aliases)['state'], 'passed')
        self.assertEqual(judge_raw(case['expected'], [], payload, aliases)['state'], 'unobserved')
        malformed = wire(raw); malformed[0]['action_json'] = '{'
        self.assertEqual(judge_raw(case['expected'], malformed, payload, aliases)['state'], 'invalid')
        raw['support'] = {'invented': 'invented quote'}
        self.assertEqual(judge_raw(case['expected'], wire(raw), payload, aliases)['reason'], 'invalid_support')

    def test_malformed_or_nonoffline_worker_envelope_is_never_raw_success(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1'))
        case = cases[0]; payload, aliases = packet(case)
        envelopes: list[object] = [None, [], {}, {'status': 'ok', 'network_attempts': True},
            {'status': 'ok', 'network_attempts': 1}, {'status': 'input_budget', 'network_attempts': 0}]
        for malformed in envelopes:
            output: Any = [malformed]
            with self.subTest(envelope=malformed):
                self.assertEqual(judge_raw(case['expected'], output, payload, aliases)['reason'], 'invalid_worker_envelope')
        self.assertEqual(judge_raw(case['expected'], wire({'action': []}), payload, aliases)['state'], 'invalid')


if __name__ == '__main__':
    unittest.main()
