from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from evernook.reflection_protocol import build_messages
from probes.admission.reflection_reasoning_ablation import GUIDANCE, PANEL, messages, verified_packet
from probes.admission.semantic_reflection import judge_generated


class ReasoningAblationTests(unittest.TestCase):
    def test_count_packets_verify_full_fixture_and_preserve_duplicate_source_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case in PANEL:
                if case.complete is None:
                    continue
                with self.subTest(case=case.id):
                    payload, aliases, originals = verified_packet(case, root / case.id)
                    evidence = payload['evidence']
                    self.assertEqual(evidence[0]['message_id'], evidence[1]['message_id'])
                    self.assertNotEqual(evidence[0]['evidence_id'], evidence[1]['evidence_id'])
                    self.assertEqual({item['message_id'] for item in evidence},
                                     {originals[alias] for alias in aliases.values()})
                    self.assertEqual(set(originals) == set(aliases.values()), case.complete)

    def test_unverified_completeness_is_rejected_before_model_request(self) -> None:
        open_case = next(case for case in PANEL if case.id == 'count-open-one')
        with TemporaryDirectory() as temporary, self.assertRaisesRegex(ValueError, 'unverified_fixture_completeness'):
            verified_packet(replace(open_case, complete=True), Path(temporary) / 'contradiction')

    def test_prompt_comparison_preserves_demo_messages_and_never_sends_gold(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case in PANEL:
                with self.subTest(case=case.id):
                    payload, _, _ = verified_packet(case, root / case.id)
                    protocol = build_messages(payload)
                    baseline = messages(payload, complete=case.complete, candidate=False)
                    candidate = messages(payload, complete=case.complete, candidate=True)
                    self.assertEqual(baseline[:-1], protocol[:-1])
                    self.assertEqual(candidate[1:], baseline[1:])
                    self.assertEqual(candidate[0]['content'], baseline[0]['content'] + GUIDANCE)
                    request = json.loads(baseline[-1]['content'])
                    if case.complete is not None:
                        self.assertEqual(request.pop('evidence_scope'), {'scope': '青羽项目登记簿',
                            'complete': case.complete, 'independent_record_key': 'message_id'})
                    self.assertEqual(request, json.loads(protocol[-1]['content']))

    def test_raw_judge_counts_sources_by_original_and_still_rejects_alias_count(self) -> None:
        case = next(case for case in PANEL if case.id == 'count-closed-two')
        with TemporaryDirectory() as temporary:
            payload, aliases, _ = verified_packet(case, Path(temporary) / case.id)
        expected = {'decision': case.decision, 'value': case.value, 'source_ids': case.support}
        raw = {'action': 'answer', 'value': '2条', 'support': {entry['evidence_id']: entry['quote'] for entry in payload['evidence']}}
        self.assertEqual(judge_generated(expected, json.dumps(raw), payload, aliases)['state'], 'passed')
        raw['value'] = '3条'
        self.assertEqual(judge_generated(expected, json.dumps(raw), payload, aliases)['state'], 'failed')
