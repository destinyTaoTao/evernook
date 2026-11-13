from __future__ import annotations

import unittest
from typing import Any

from probes.admission.reflection_projection import project_result


class ReflectionCollectionTests(unittest.TestCase):
    def test_real_abstention_reason_citation_is_not_lost_or_inferred_from_retrieval(self) -> None:
        evidence = {'evidence_id': 'e7', 'message_id': 'actual', 'start': 0, 'end': 3, 'quote': '未知年', 'state_version': 1, 'content_version': 1}
        result: dict[str, Any] = {'state': 'complete', 'evidence': [evidence], 'conclusions': [], 'conflicts': [], 'clarification_required': True,
            'assessment': {'decision': 'abstain', 'support': [{'evidence_id': 'e7', 'quote': '未知年'}], 'origin': 'model'}}
        projected = project_result(result, {'actual': 'fixture-alias'})
        self.assertEqual(projected['judgment'], {'decision': 'abstain', 'value': None, 'source_ids': ['fixture-alias']})
        result['assessment'] = None
        self.assertEqual(project_result(result, {'actual': 'fixture-alias'})['judgment']['source_ids'], [])

    def test_refusal_and_rule_origin_are_not_relabelled_as_model_answers(self) -> None:
        result: dict[str, Any] = {'state': 'complete', 'evidence': [], 'conclusions': [], 'conflicts': [], 'clarification_required': False,
            'assessment': {'decision': 'refuse', 'support': [], 'origin': 'model'}}
        self.assertEqual(project_result(result, {})['judgment']['decision'], 'refuse')


if __name__ == '__main__':
    unittest.main()
