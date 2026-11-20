from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook import MemoryRuntime, SemanticRecall
from evernook.models import MemoryFailure
from evernook.reflection import ReflectionEngine
from evernook.reflection_model import from_file
from probes.admission.reflect import collect
from probes.admission.reflection_projection import project_result


def fixture_profile(root: Path) -> Path:
    profile = root / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 1, 'profile': 'qwen3-0.6b-experimental-reflection-v1',
        'node': str(root / 'not-used.exe'), 'transformers_dir': str(root / 'not-used'),
        'model_dir': str(root / 'not-used'), 'policy': {}}), encoding='utf8')
    return profile


class ReflectionCollectionTests(unittest.TestCase):
    def test_real_abstention_reason_citation_is_not_lost_or_inferred_from_retrieval(self) -> None:
        evidence = {'evidence_id': 'e7', 'message_id': 'actual', 'start': 0, 'end': 3, 'quote': '未知年', 'state_version': 1, 'content_version': 1}
        result: dict[str, Any] = {'state': 'complete', 'evidence': [evidence], 'conclusions': [], 'conflicts': [], 'clarification_required': True,
            'assessment': {'decision': 'abstain', 'support': [{'evidence_id': 'e7', 'quote': '未知年'}], 'origin': 'model'}}
        projected = project_result(result, {'actual': 'fixture-alias'})
        self.assertEqual(projected['judgment'], {'decision': 'abstain', 'value': None, 'source_ids': ['fixture-alias']})
        result['assessment'] = None
        self.assertNotIn('judgment', project_result(result, {'actual': 'fixture-alias'}))

    def test_incomplete_generation_never_scores_as_abstention(self) -> None:
        result: dict[str, Any] = {'state': 'partial', 'stop_reason': 'reflection_model_round_budget',
            'evidence': [], 'conclusions': [], 'conflicts': [], 'clarification_required': True, 'assessment': None,
            'coverage': [{'tool': 'search', 'candidates': 0}]}
        self.assertEqual(project_result(result, {}), {})

    def test_partial_projection_uses_delivered_evidence_after_final_handoff(self) -> None:
        result: dict[str, Any] = {'state': 'partial', 'stop_reason': 'reflection_timeout',
            'coverage': [{'tool': 'search', 'candidates': 1}], 'evidence': [], 'assessment': None}
        aliases = {'actual': 'fixture-alias'}
        for reason in ('reflection_timeout', 'memory_changed_during_reflection'):
            with self.subTest(reason=reason):
                result['stop_reason'] = reason
                self.assertEqual(project_result(result, aliases), {})
        result['evidence'] = [{'evidence_id': 'e1', 'message_id': 'actual', 'start': 0, 'end': 3,
            'quote': '未知年', 'state_version': 1, 'content_version': 1}]
        self.assertEqual(project_result(result, aliases), {
            'retrieval': {'source_ids': ['fixture-alias']},
            'evidence': {'citations': [{'source_id': 'fixture-alias', 'start': 0, 'end': 3, 'quote': '未知年',
                                       'state_version': 1, 'content_version': 1}]}})

    def test_explicit_protected_routing_is_a_core_refusal(self) -> None:
        result: dict[str, Any] = {'state': 'protected_route_required', 'evidence': [], 'assessment': None, 'coverage': []}
        self.assertEqual(project_result(result, {}), {'judgment': {'decision': 'refuse', 'value': None, 'source_ids': []}})

    def test_refusal_and_rule_origin_are_not_relabelled_as_model_answers(self) -> None:
        result: dict[str, Any] = {'state': 'complete', 'evidence': [], 'conclusions': [], 'conflicts': [], 'clarification_required': False,
            'assessment': {'decision': 'refuse', 'support': [], 'origin': 'model'}}
        self.assertEqual(project_result(result, {})['judgment']['decision'], 'refuse')

    def test_selected_later_phases_replay_fixture_history_before_real_core_reflection(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-reflect-lifecycle-') as temporary:
            root = Path(temporary)
            profile = fixture_profile(root)
            output = root / 'collection'
            cases = ('old-replay', 'old-processor', 'control-restored', 'purge-continued')
            # Exercise the public engine and real lifecycle protocols without local model assets.
            with patch('evernook.reflection_model.LocalReflectionModel.turn', side_effect=MemoryFailure('reflection_model_unavailable')):
                report = collect(Path('eval/zh-learning-v1'), output, profile, None, cases, full_lifecycle=True)
            details = {row['case_id']: row for row in json.loads((output / 'details.json').read_text(encoding='utf8'))}
            observations = json.loads((output / 'observations.json').read_text(encoding='utf8'))
            rows = {row['case_id']: row['payload'] for row in observations['rows']}
            self.assertEqual(set(rows), set(cases) - {'old-processor'})
            self.assertFalse(details['old-initial']['selected'])
            self.assertTrue(details['old-replay']['receipts'][0]['replayed_same_id'])
            self.assertEqual(details['old-processor']['missing_reason'], 'reflection_processor_transition_not_collected')
            self.assertEqual(details['control-restored']['lifecycle']['operation'], 'control_replayed_runtime')
            self.assertEqual(details['purge-continued']['controls'][0]['state'], 'completed')
            self.assertNotIn('suppressed', rows['control-restored']['retrieval']['source_ids'])
            self.assertNotIn('purge-source', rows['purge-continued']['retrieval']['source_ids'])
            self.assertNotIn('judgment', rows['old-replay'])
            self.assertEqual(details['old-replay']['judgment_missing_reason'], 'reflection_model_unavailable')
            self.assertEqual(report['arms']['reflect']['judgment']['passed'], 0)

    def test_profile_change_during_engine_load_fails_before_invocation(self) -> None:
        def load_then_change(runtime: MemoryRuntime, recall: SemanticRecall, path: Path) -> ReflectionEngine:
            loaded = from_file(runtime, recall, path)
            path.write_bytes(path.read_bytes() + b'\n')
            return loaded

        with tempfile.TemporaryDirectory(prefix='evernook-reflect-profile-drift-') as temporary:
            root = Path(temporary)
            profile = fixture_profile(root)
            output = root / 'collection'
            with patch('probes.admission.reflect.from_file', side_effect=load_then_change), self.assertRaisesRegex(
                    RuntimeError, 'reflection_model_config_changed_during_collection'):
                collect(Path('eval/zh-learning-v1'), output, profile, None, ('old-initial',))
            self.assertFalse((output / 'old-initial-workers').exists())


if __name__ == '__main__':
    unittest.main()
