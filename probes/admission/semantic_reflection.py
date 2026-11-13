"""Frozen synthetic provided-evidence diagnostics; no retrieval or rule-based semantic credit."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from unittest.mock import patch

from evernook.models import MemoryFailure
from evernook.extraction import LocalQwen
from evernook.paths import external
from evernook.reflection_model import LocalReflectionModel


def load_cases(root: Path) -> tuple[list[dict[str, Any]], str]:
    data = (root / 'cases.json').read_bytes()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(data).hexdigest()
    if digest != manifest['cases_sha256']:
        raise ValueError('semantic_cases_hash_mismatch')
    cases = json.loads(data)
    if not isinstance(cases, list) or not 1 <= len(cases) <= 16 or len({row['id'] for row in cases}) != len(cases):
        raise ValueError('invalid_semantic_cases')
    for row in cases:
        if (set(row) != {'id', 'category', 'question', 'sources', 'expected'} or not isinstance(row['question'], str)
                or not 1 <= len(row['question']) <= 256 or not 1 <= len(row['sources']) <= 8
                or len({item['id'] for item in row['sources']}) != len(row['sources'])):
            raise ValueError('invalid_semantic_case')
        for item in row['sources']:
            if (set(item) != {'id', 'role', 'text'} or item['role'] not in {'user', 'assistant', 'tool'}
                    or not isinstance(item['text'], str) or not 1 <= len(item['text']) <= 512):
                raise ValueError('invalid_semantic_source')
        expected = row['expected']
        if (set(expected) != {'decision', 'value', 'source_ids'} or expected['decision'] not in {'answer', 'abstain', 'clarify', 'refuse'}
                or not set(expected['source_ids']).issubset(item['id'] for item in row['sources'])):
            raise ValueError('invalid_semantic_expectation')
    return cases, digest


def packet(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    evidence, aliases = [], {}
    for index, item in enumerate(case['sources'], 1):
        ref = 'e' + str(index)
        aliases[ref] = item['id']
        evidence.append({'evidence_id': ref, 'message_id': 'synthetic-source-' + str(index), 'role': item['role'],
            'quote': item['text'], 'occurred_at': None})
    return {'question': case['question'], 'evidence': evidence, 'allowed_view_ids': []}, aliases


def judge(expected: dict[str, Any], action: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    if action.get('action') != 'done' or not isinstance(action.get('assessment'), dict):
        return {'state': 'failed', 'reason': 'no_final_model_decision', 'unsupported_answer': False}
    assessment = action['assessment']
    if assessment['origin'] != 'model':
        return {'state': 'guarded', 'reason': 'not_model_semantic_credit', 'unsupported_answer': None}
    actual = {'decision': assessment['decision'], 'value': '\n'.join(item['text'] for item in action['conclusions']) or None,
        'source_ids': sorted({aliases[item['evidence_id']] for item in assessment['support']})}
    passed = actual == {**expected, 'source_ids': sorted(expected['source_ids'])}
    return {'state': 'passed' if passed else 'failed', 'actual': actual,
        'unsupported_answer': expected['decision'] != 'answer' and actual['decision'] == 'answer',
        'false_abstention': expected['decision'] == 'answer' and actual['decision'] != 'answer'}


def judge_raw(expected: dict[str, Any], wire: list[dict[str, Any]], payload: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    """Score the generated action before translation, guards or source rejection.

    Invalid citations cannot hide an affirmative answer to an unsupported claim.
    Timeouts with no observed output are unobserved, never semantic success.
    """
    if not wire:
        return {'state': 'unobserved', 'reason': 'no_completed_worker_output', 'unsupported_answer': None}
    envelope = wire[-1]
    if (not isinstance(envelope, dict) or envelope.get('status') != 'ok'
            or type(envelope.get('network_attempts')) is not int or envelope['network_attempts'] != 0):
        return {'state': 'invalid', 'reason': 'invalid_worker_envelope', 'unsupported_answer': None}
    try:
        action = LocalQwen.parse_selection(envelope['action_json'])
    except (MemoryFailure, KeyError):
        return {'state': 'invalid', 'reason': 'invalid_json', 'unsupported_answer': None}
    if not isinstance(action, dict):
        return {'state': 'invalid', 'reason': 'invalid_action', 'unsupported_answer': None}
    decision = action.get('action')
    assertion = expected['decision'] != 'answer' and decision == 'answer'
    fields = {'action', 'value', 'support'} if decision == 'answer' else {'action', 'support'}
    if (not isinstance(decision, str) or decision not in {'answer', 'abstain', 'clarify', 'refuse'} or set(action) != fields
            or decision == 'answer' and (not isinstance(action.get('value'), str) or not action['value'].strip())):
        return {'state': 'invalid', 'reason': 'invalid_action', 'unsupported_answer': assertion}
    sources = {item['evidence_id']: item['quote'] for item in payload['evidence']}
    support = action['support']
    literal_support = (isinstance(support, dict) and all(ref in sources and isinstance(quote, str)
        and bool(quote) and quote in sources[ref] for ref, quote in support.items())
        and (bool(support) or decision == 'abstain'))
    if not literal_support:
        return {'state': 'invalid', 'reason': 'invalid_support', 'decision': decision, 'unsupported_answer': assertion}
    actual = {'decision': decision, 'value': action.get('value'), 'source_ids': sorted({aliases[ref] for ref in support})}
    return {'state': 'passed' if actual == {**expected, 'source_ids': sorted(expected['source_ids'])} else 'failed',
        'actual': actual, 'unsupported_answer': assertion,
        'false_abstention': expected['decision'] == 'answer' and decision != 'answer'}


def collect(root: Path, output: Path, model_config: Path) -> dict[str, Any]:
    collector_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    cases, digest = load_cases(root)
    destination = external(output); destination.mkdir()
    config = json.loads(external(model_config).read_text(encoding='utf-8'))
    model = LocalReflectionModel(node=Path(config['node']), transformers_dir=Path(config['transformers_dir']), model_dir=Path(config['model_dir']))
    rows = []
    for case in cases:
        payload, aliases = packet(case)
        wire: dict[int, dict[str, Any]] = {}
        inputs: dict[int, dict[str, Any]] = {}
        class TraceProcess(subprocess.Popen[bytes]):
            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                if input and 'reflection_worker.mjs' in str(self.args):
                    messages = json.loads(input)['messages']
                    inputs[self.pid] = {'messages': messages, 'request_sha256': hashlib.sha256(input).hexdigest()}
                value, error = super().communicate(input=input, timeout=timeout)
                if value and 'reflection_worker.mjs' in str(self.args):
                    wire[self.pid] = json.loads(value)
                return value, error
        started = time.monotonic()
        try:
            with patch('evernook.reflection_model.subprocess.Popen', TraceProcess):
                turn = model.turn(payload, input_tokens=4096, output_tokens=512, timeout=30, cancel=None)
            outcome = judge(case['expected'], turn.action, aliases)
            row = {'case_id': case['id'], 'category': case['category'], 'action': turn.action, 'judgment': outcome,
                'input_tokens': turn.input_tokens, 'output_tokens': turn.output_tokens}
        except MemoryFailure as error:
            row = {'case_id': case['id'], 'category': case['category'], 'judgment': {'state': 'failed', 'reason': error.code}, 'metering_incomplete': True}
        outputs = list(wire.values())
        row.update(elapsed_ms=(time.monotonic() - started) * 1000, model_wire=outputs,
            model_inputs=list(inputs.values()), raw_judgment=judge_raw(case['expected'], outputs, payload, aliases))
        rows.append(row)
        (destination / 'rows.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print({'case_id': case['id'], 'judgment': row['judgment']}, flush=True)
    result = {'cases_sha256': digest, 'processor': model.processor_version, 'rows': rows,
        'totals': {state: sum(row['judgment']['state'] == state for row in rows) for state in ('passed', 'failed', 'guarded')},
        'raw_totals': {state: sum(row['raw_judgment']['state'] == state for row in rows) for state in ('passed', 'failed', 'invalid', 'unobserved')},
        'scope': 'One local model turn on supplied complete synthetic evidence; no retrieval, host answer or permission admission. No credit for rule-modified decisions.',
        'budget': {'seconds': 30, 'input_tokens': 4096, 'output_tokens': 512},
        'collector_sha256': collector_digest,
        'collector_unchanged': hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == collector_digest, 'admission': 'pending'}
    (destination / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-reflection-semantics-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    args = parser.parse_args()
    collect(args.suite, args.output, args.model_config)
