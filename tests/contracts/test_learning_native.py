from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from typing import Any

from evernook.learning_evaluation import load_suite, score
from probes.admission.learning_native import combine, inputs


class LearningNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = load_suite(Path('eval/zh-learning-v1'))
        source = self.suite.sources['old']
        self.observations: dict[str, Any] = {'suite_hash': self.suite.suite_hash, 'rows': [
            {'case_id': 'old-initial', 'arm': 'raw', 'payload': {'retrieval': {'source_ids': ['old']}}}]}
        self.details: list[dict[str, Any]] = [{'case_id': 'old-initial', 'recall': {'hits': [{'evidence': {
            'message_id': 'synthetic-actual-id', 'role': source['role'], 'occurred_at': source['at'],
            'spans': [{'start': 0, 'end': 2, 'text': source['text'][:2]}]}}]}}]

    def test_only_delivered_spans_and_original_roles_times_enter_model(self) -> None:
        before = deepcopy(self.observations)
        cases, packets = inputs(self.suite, self.observations, self.details)
        payload, aliases = packets['old-initial']
        self.assertEqual(payload['evidence'][0]['quote'], self.suite.sources['old']['text'][:2])
        self.assertEqual(payload['evidence'][0]['occurred_at'], self.suite.sources['old']['at'])
        self.assertNotIn('expected', payload)
        self.assertEqual(aliases, {'e1': 'old'})
        self.assertEqual(cases[0]['expected'], self.suite.cases['old-initial']['expected']['judgment'])
        self.assertEqual(self.observations, before)

    def test_missing_retrieval_is_not_filled_from_expected_sources(self) -> None:
        self.observations['rows'][0]['payload']['retrieval']['source_ids'] = []
        self.details[0]['recall']['hits'] = []
        _, packets = inputs(self.suite, self.observations, self.details)
        self.assertEqual(packets['old-initial'][0]['evidence'], [])

    def test_mismatched_original_or_role_rejected_before_inference(self) -> None:
        for field, value in (('role', 'assistant'), ('occurred_at', '2026-01-01T00:00:00Z')):
            changed = deepcopy(self.details)
            changed[0]['recall']['hits'][0]['evidence'][field] = value
            with self.assertRaisesRegex(ValueError, 'provenance_mismatch'):
                inputs(self.suite, self.observations, changed)
        self.details[0]['recall']['hits'][0]['evidence']['spans'][0]['text'] = '伪造'
        with self.assertRaisesRegex(ValueError, 'quote_mismatch'):
            inputs(self.suite, self.observations, self.details)

    def test_projection_preserves_actual_failure_and_valid_frozen_envelope(self) -> None:
        observations = deepcopy(self.observations)
        observations['configuration'] = {'model': 'synthetic retrieval', 'processor': 'synthetic rules',
            'budget': {'candidate_k': 10}, 'policy_sha256': self.suite.manifest['policy_sha256'], 'implementation': 'synthetic'}
        observations['rows'][0]['measurements'] = {'elapsed_ms': 1.0}
        actual: dict[str, Any] = {'decision': 'abstain', 'value': None, 'source_ids': []}
        native = {'identity': {'synthetic': 'fixture'}, 'rows': [
            {'case_id': 'old-initial', 'raw_judgment': {'state': 'failed', 'actual': actual}}]}
        combined = combine(observations, native)
        self.assertEqual(combined['rows'][0]['payload']['judgment'], actual)
        self.assertNotIn('host_answer', combined['rows'][0]['payload'])
        self.assertIsNone(combined['rows'][0]['measurements']['elapsed_ms'])
        self.assertEqual(score(self.suite, combined)['arms']['raw']['judgment'],
                         {'expected': 33, 'passed': 0, 'failed': 1, 'missing': 32})
        self.assertNotIn('judgment', observations['rows'][0]['payload'])
