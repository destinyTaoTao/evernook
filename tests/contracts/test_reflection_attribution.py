from pathlib import Path
import json
from typing import Any
import unittest

from evernook.learning_evaluation import LearningSuite, load_suite
from probes.admission.reflection_attribution import classify, inspect_turn


class AttributionTests(unittest.TestCase):
    suite: LearningSuite

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = load_suite(Path(__file__).resolve().parents[2] / 'eval/zh-learning-v1')

    def test_only_the_current_packet_counts_and_quotes_need_the_receipted_source(self) -> None:
        case = self.suite.cases['style-continued']
        source = self.suite.sources['style']
        evidence = {'e1': {'message_id': 'receipt', 'quote': source['text'],
            'role': source['role'], 'statement_at': source['at']}}
        request = {'messages': [{'role': 'user', 'content': json.dumps({'question': 'demo',
            'evidence': {'demo': self.suite.sources['exception']}})}, {'role': 'user',
            'content': json.dumps({'question': case['question'], 'evidence': evidence, 'format_feedback': 'invalid_support'})}]}
        wire = {'status': 'ok', 'action_json': json.dumps({'action': 'answer', 'value': '简短',
            'support': {'e1': '一段原文中没有的引述'}})}
        turn = inspect_turn(self.suite, case, {'receipt': 'style'}, request, wire)
        self.assertEqual(turn['missing_quotes'], ['exception'])
        self.assertFalse(turn['support_is_verbatim'])
        self.assertEqual(turn['feedback'], 'invalid_support')
        with self.assertRaisesRegex(ValueError, 'packet_source_mismatch'):
            inspect_turn(self.suite, case, {'receipt': 'exception'}, request, wire)

    def test_zero_candidates_and_partial_evidence_have_different_attributions(self) -> None:
        states = {'judgment': 'failed', 'evidence': 'failed'}
        case = self.suite.cases['style-continued']
        search = {'tool': 'search', 'candidates': 0}
        detail = {'judgment_origin': 'model', 'result': {'coverage': [search]}}
        turn: dict[str, Any] = {'action': {'action': 'abstain'}, 'sent': {}, 'missing_quotes': ['style', 'exception']}
        self.assertEqual(classify(states, detail, case, [turn]), 'zero_recall')
        search['candidates'] = 1
        self.assertEqual(classify(states, detail, case, [turn]), 'incomplete_final_packet')
        self.assertEqual(classify(states, detail, case, [{'trace_unavailable': True}]), 'unknown')

    def test_guard_and_timeout_do_not_become_model_judgments(self) -> None:
        case = self.suite.cases['interest-continued']
        states = {'judgment': 'failed', 'evidence': 'failed'}
        detail = {'judgment_origin': 'finite_preference_guard', 'result': {}}
        self.assertEqual(classify(states, detail, case, []), 'guard')
        self.assertEqual(classify({**states, 'judgment': 'missing'},
            {'judgment_missing_reason': 'reflection_timeout'}, case, []), 'timeout')

    def test_extra_control_material_does_not_mean_a_purged_source_returned(self) -> None:
        case = self.suite.cases['purge-continued']
        states = {'judgment': 'failed', 'evidence': 'failed'}
        turn = {'action': {'action': 'answer'}, 'sent': {'e1': {'source': 'purge-confirm'}}, 'missing_quotes': []}
        self.assertEqual(classify(states, {'judgment_origin': 'model'}, case, [turn]), 'extra_context_and_wrong_judgment')
