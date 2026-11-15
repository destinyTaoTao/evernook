"""Single-turn native-model diagnostics using actual frozen-suite retrieval receipts."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from evernook.learning_evaluation import LearningSuite, load_suite, score
from evernook.paths import external
from probes.admission import local_reflection
from probes.admission.abstention_diagnostics import diagnostic_messages
from probes.admission.learning import save


def inputs(suite: LearningSuite, observations: dict[str, Any], details: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], dict[str, tuple[dict[str, Any], dict[str, str]]]]:
    if observations['suite_hash'] != suite.suite_hash:
        raise ValueError('learning_source_identity_mismatch')
    by_id = {row['case_id']: row for row in details}
    cases, packets = [], {}
    for row in observations['rows']:
        key = row['case_id']
        expected = suite.cases[key]['expected']['judgment']
        if row['arm'] != 'raw' or expected['decision'] not in ('answer', 'abstain', 'refuse', 'clarify'):
            continue
        detail = by_id[key]
        if 'recall' not in detail:
            continue
        hits = detail['recall']['hits']
        aliases = row['payload']['retrieval']['source_ids']
        evidence: list[dict[str, Any]] = []
        bindings: dict[str, str] = {}
        for hit, alias in zip(hits, aliases, strict=True):
            source = suite.sources[alias]
            item = hit['evidence']
            if item['role'] != source['role'] or item['occurred_at'] != source['at']:
                raise ValueError('learning_source_provenance_mismatch')
            for span in item['spans']:
                if source['text'][span['start']:span['end']] != span['text']:
                    raise ValueError('learning_source_quote_mismatch')
                ref = 'e' + str(len(evidence) + 1)
                evidence.append({'evidence_id': ref, 'message_id': item['message_id'], 'role': item['role'],
                    'quote': span['text'], 'occurred_at': item['occurred_at']})
                bindings[ref] = alias
        if len(evidence) > 32:
            raise ValueError('learning_native_evidence_budget')
        cases.append({'id': key, 'expected': deepcopy(expected), 'sources': []})
        packets[key] = ({'question': suite.cases[key]['question'], 'evidence': evidence, 'allowed_view_ids': []}, bindings)
    return cases, packets


def combine(observations: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    actuals = {row['case_id']: row for row in report['rows']}
    combined = deepcopy(observations)
    combined['configuration']['model'] += '; native single-turn qwen35-2b-q4 role_boundary_minimal_value'
    combined['configuration']['budget']['native_diagnostic'] = {'single_turn': True, 'input_tokens': 4096,
        'output_tokens': 512, 'seconds': 30}
    combined['configuration']['implementation'] = hashlib.sha256(json.dumps({
        'retrieval': observations['configuration']['implementation'], 'native': report['identity'],
        'projection': local_reflection.digest(Path(__file__))}, sort_keys=True).encode()).hexdigest()
    for row in combined['rows']:
        if row['arm'] == 'raw' and row['case_id'] in actuals:
            actual = actuals[row['case_id']]['raw_judgment'].get('actual')
            if actual is not None:
                row['payload']['judgment'] = actual
            # The retrieval and inference windows were collected separately.
            row['measurements']['elapsed_ms'] = None
    return combined


def collect(source: Path, output: Path, assets: Path, node: Path, jinja: Path) -> dict[str, Any]:
    source, output = external(source), external(output)
    suite = load_suite(Path('eval/zh-learning-v1'))
    observations = json.loads((source / 'observations.json').read_text(encoding='utf8'))
    details = json.loads((source / 'details.json').read_text(encoding='utf8'))
    cases, packets = inputs(suite, observations, details)
    output.mkdir()
    save(output / 'manifest.json', {'suite_hash': suite.suite_hash, 'driver_sha256': local_reflection.digest(Path(__file__)),
        'source_hashes': {name: local_reflection.digest(source / name) for name in ('observations.json', 'details.json')},
        'policy_variant': 'role_boundary_minimal_value', 'cases': [case['id'] for case in cases],
        'scope': 'Actual retrieved spans only. No oracle evidence supplementation, host answer, automatic admission or model action execution.'})
    save(output / 'model-inputs.json', {key: value[0] for key, value in packets.items()})
    case_hash = hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with patch.object(local_reflection, 'load_cases', return_value=(cases, case_hash)), patch.object(
            local_reflection, 'packet', side_effect=lambda case: packets[case['id']]), patch.object(
            local_reflection, 'build_messages', side_effect=lambda payload: diagnostic_messages(payload, 'role_boundary_minimal_value')):
        report = local_reflection.collect(assets, 'qwen35-2b-q4', output / 'native', node, jinja)
    combined = combine(observations, report)
    report.update(quality_screen=False, admission='not_applicable_diagnostic',
        scope='Frozen learning-suite single-turn actual retrieval diagnostic, not the 10-case screen or production host integration.')
    save(output / 'native/report.json', report)
    save(output / 'observations.json', combined)
    result = score(suite, combined)
    save(output / 'report.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'assets', 'node', 'jinja'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    collect(args.source, args.output, args.assets, args.node, args.jinja)
