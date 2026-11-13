from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probes.admission.routing_diagnostics import collect, final_schema
from probes.admission.semantic_reflection import load_cases, packet
from probes.admission.structured_reflection import schema


class RoutingDiagnosticTests(unittest.TestCase):
    def test_removing_tools_is_explicitly_a_counterfactual_not_a_semantic_rule(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1'))
        payload, _ = packet(cases[0])
        before = json.dumps(payload, sort_keys=True)
        original = schema(payload, 'flat')
        final = final_schema(payload, 'flat', proof_first=False)
        self.assertEqual(final['oneOf'], original['oneOf'][:2])
        proof = final_schema(payload, 'flat', proof_first=True)
        self.assertEqual(json.dumps(final, sort_keys=True), json.dumps(proof, sort_keys=True))
        self.assertEqual(before, json.dumps(payload, sort_keys=True))

    def test_restricted_control_cannot_accidentally_earn_admission(self) -> None:
        _, source_hash = load_cases(Path('eval/zh-reflection-semantics-v1'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('probes.admission.routing_diagnostics.native.collect', return_value={
                    'cases_sha256': source_hash, 'identity': {}, 'collector_unchanged': True,
                    'quality_screen': True, 'admission': 'pending'}):
                result = collect(root, root, root / 'node', root / 'jinja', 'final-proof')
            self.assertFalse(result['quality_screen'])
            self.assertEqual(result['admission'], 'not_applicable_counterfactual')
            self.assertIn('source_cases_sha256', result)
