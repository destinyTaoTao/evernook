"""Counterfactual controls only. Restricted actions/ablated sources never earn admission."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from probes.admission import local_reflection as native
from probes.admission.evidence_order import ordered_schema
from probes.admission.semantic_reflection import load_cases, packet
from probes.admission.structured_reflection import schema


def final_schema(payload: dict[str, Any], wire: str, *, proof_first: bool) -> dict[str, Any]:
    result = ordered_schema(payload, wire) if proof_first else schema(payload, wire)
    result['oneOf'] = [item for item in result['oneOf'] if 'support' in item['properties']]
    return result


def collect(assets: Path, output: Path, node: Path, jinja: Path, condition: str) -> dict[str, Any]:
    if condition not in {'final-flat', 'final-proof', 'ablation'}:
        raise ValueError('invalid_diagnostic_condition')
    driver = Path(__file__).resolve()
    identity = native.digest(driver)
    cases, source_hash = load_cases(Path(__file__).resolve().parents[2] / 'eval/zh-reflection-semantics-v1')
    selected = deepcopy(cases)
    if condition == 'ablation':
        selected = [{**deepcopy(case), 'id': case['id'] + '__without_e' + str(index)} for case in cases[:3] for index in (1, 2)]
    def prepared(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        payload, aliases = packet(case)
        if condition == 'ablation':
            removed = case['id'].rsplit('__without_', 1)[1]
            payload['evidence'] = [item for item in payload['evidence'] if item['evidence_id'] != removed]
        return payload, aliases
    def constrained(payload: dict[str, Any], wire: str) -> dict[str, Any]:
        return final_schema(payload, wire, proof_first=condition == 'final-proof')
    with patch.object(native, 'load_cases', return_value=(selected, source_hash)), patch.object(native, 'packet', prepared), \
            patch.object(native, 'schema', constrained):
        result = native.collect(assets, 'qwen35-4b-iq3xxs' if condition == 'ablation' else 'qwen35-2b-q4',
            output, node, jinja, constrained=condition != 'ablation', compact_memory=condition == 'ablation')
    result['identity'][driver.name] = identity
    result['identity']['evidence_order.py'] = native.digest(driver.with_name('evidence_order.py'))
    result['collector_unchanged'] = result['collector_unchanged'] and native.digest(driver) == identity
    result['source_cases_sha256'] = result.pop('cases_sha256')
    result['diagnostic_condition'] = condition
    result['diagnostic_cases_sha256'] = hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    result['quality_screen'] = False
    result['admission'] = 'not_applicable_counterfactual'
    result['scope'] = 'Restricted final-action decoding or deleted original sources, diagnostic only; no change to gold or scoring. No production authorization/availability policy.'
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'output', 'node', 'jinja'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--condition', choices=('final-flat', 'final-proof', 'ablation'), required=True)
    args = parser.parse_args()
    collect(args.assets, args.output, args.node, args.jinja, args.condition)
