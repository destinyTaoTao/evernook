from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from evernook.reflection_protocol import build_messages
from probes.admission.abstention_diagnostics import diagnostic_cases, diagnostic_messages, NEGATIVE
from probes.admission.semantic_reflection import load_cases, packet


class AbstentionDiagnosticTests(unittest.TestCase):
    def test_counterfactuals_do_not_mutate_frozen_inputs_or_expectations(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1')); before = deepcopy(cases)
        changed = diagnostic_cases(cases, 'role_user')
        self.assertEqual(len(changed), 2)
        self.assertTrue(all(source['role'] == 'user' for case in changed for source in case['sources']))
        self.assertEqual(cases, before)
        for row in changed:
            original = next(case for case in cases if case['id'] == row['id'])
            self.assertEqual(row['expected'], original['expected'])
            self.assertEqual(row['question'], original['question'])
        controls = diagnostic_cases(cases, 'remove_source_trust_sentence')
        self.assertTrue(NEGATIVE.issubset(row['id'] for row in controls))

    def test_ablation_only_deletes_one_known_system_span_and_preserves_actual_payload(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1')); payload, _ = packet(cases[0])
        original = build_messages(payload)
        self.assertEqual(diagnostic_messages(payload, 'baseline'), original)
        for name in ('remove_empty_example', 'remove_source_trust_sentence'):
            changed = diagnostic_messages(payload, name)
            self.assertEqual(changed[1:], original[1:])
            self.assertLess(len(changed[0]['content']), len(original[0]['content']))
        with self.assertRaises(ValueError):
            diagnostic_cases(cases, 'invented')
