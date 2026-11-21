"""Read-only postmortem of a frozen synthetic reflect campaign. Never calls a model."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from evernook.learning_evaluation import LearningSuite, load_suite, score

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from probes.admission.reflection_projection import project_result


def inspect_turn(suite: LearningSuite, case: dict[str, Any], aliases: dict[str, str],
                 request: dict[str, Any], wire: dict[str, Any] | None) -> dict[str, Any]:
    # Earlier messages include demonstrations with their own questions, IDs and answers.
    message = request['messages'][-1]
    packet = json.loads(message['content'])
    if message['role'] != 'user' or packet['question'] != case['question']:
        raise ValueError('question_or_role_mismatch')
    items = packet['evidence']
    quotes = case['expected']['evidence']['required_quotes']
    visible, matched = {}, set()
    for slot, entry in items.items():
        alias = aliases[entry['message_id']]
        source = suite.sources[alias]
        if (alias not in case['available_sources'] or not entry['quote'] or entry['quote'] not in source['text']
                or entry['role'] != source['role'] or entry.get('statement_at') != source['at']):
            raise ValueError('packet_source_mismatch')
        visible[slot] = {'source': alias, 'quote': entry['quote'], 'role': entry['role']}
        if alias in quotes and quotes[alias] in entry['quote']:
            matched.add(alias)
    action = None
    if wire and wire.get('status') == 'ok':
        try:
            parsed = json.loads(wire['action_json'])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            action = parsed
    support = action.get('support') if action else None
    exact = (all(slot in items and isinstance(quote, str) and quote and quote in items[slot]['quote']
        for slot, quote in support.items()) if isinstance(support, dict) else None)
    return {'sent': visible, 'missing_quotes': sorted(quotes.keys() - matched), 'feedback': packet.get('format_feedback'),
        'worker_status': wire.get('status') if wire else None, 'action': action, 'support_is_verbatim': exact}


def classify(states: dict[str, str], detail: dict[str, Any], case: dict[str, Any],
             turns: list[dict[str, Any]]) -> str:
    if states['judgment'] == 'passed':
        return 'strict_pass'
    if states['judgment'] == 'missing':
        if detail.get('judgment_missing_reason') == 'reflection_timeout':
            return 'timeout'
        return 'uncollected' if 'result' not in detail else 'unknown'
    if detail.get('judgment_origin') != 'model':
        return 'guard' if detail.get('judgment_origin') else 'unknown'
    if not turns or any(turn.get('trace_unavailable') for turn in turns):
        return 'unknown'
    last = turns[-1]
    if not last['action']:
        return 'unknown'
    if last['missing_quotes']:
        counts = [row['candidates'] for row in detail['result']['coverage'] if row['tool'] == 'search']
        if counts and not any(counts) and not any(turn['sent'] for turn in turns):
            return 'zero_recall'
        return 'incomplete_final_packet'
    if not case['expected']['evidence']['required_quotes'] and last['sent'] and states['evidence'] == 'failed':
        return 'extra_context_and_wrong_judgment'
    return 'model_with_expected_quotes'


def diagnose(suite: LearningSuite, root: Path) -> dict[str, Any]:
    def read(name: str) -> Any:
        return json.loads((root / name).read_bytes())

    observations = read('observations.json')
    report = read('report.json')
    if report != score(suite, observations):
        raise ValueError('frozen_report_mismatch')
    details = read('details.json')
    if [row['case_id'] for row in details] != [step['id'] for step in suite.steps]:
        raise ValueError('incomplete_or_reordered_campaign')
    aliases: dict[str, str] = {}
    for detail in details:
        for item in detail.get('receipts', []):
            if aliases.setdefault(item['receipt']['message_id'], item['source_id']) != item['source_id']:
                raise ValueError('conflicting_source_receipts')
    states: dict[str, dict[str, str]] = {}
    for cell in report['results']:
        if cell['arm'] == 'reflect':
            states.setdefault(cell['case_id'], {})[cell['dimension']] = cell['state']
    rows = {row['case_id']: row['payload'] for row in observations['rows'] if row['arm'] == 'reflect'}
    cases = []
    trace_hash = hashlib.sha256()
    for detail in details:
        key = detail['case_id']
        case = suite.cases[key]
        actual = rows.get(key)
        if 'result' in detail and project_result(detail['result'], aliases) != actual:
            raise ValueError('delivered_projection_mismatch')
        turns = []
        wires = []
        for index, attempt in enumerate(detail.get('model_attempts', []), 1):
            if attempt['transcript'] != f'turn-{index}':
                raise ValueError('turn_sequence_mismatch')
            folder = root / (key + '-workers') / f'turn-{index}'
            blobs = {}
            for name in ('stdin.bin', 'stdout.bin'):
                path = folder / name
                if path.is_file():
                    blobs[name] = path.read_bytes()
                    trace_hash.update(path.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(blobs[name]).digest())
            try:
                wire = json.loads(blobs['stdout.bin']) if blobs.get('stdout.bin') else None
            except ValueError:
                wire = None
            if isinstance(wire, dict):
                wires.append(wire)
            if len(blobs) != 2:
                turns.append({'trace_unavailable': True})
                continue
            turn = inspect_turn(suite, case, aliases, json.loads(blobs['stdin.bin']), wire)
            turns.append({**turn, 'returncode': attempt['returncode']})
        if not any(turn.get('trace_unavailable') for turn in turns) and wires != detail.get('model_wire', []):
            raise ValueError('worker_receipt_mismatch')
        result = detail.get('result', {})
        cases.append({'case_id': key, 'states': {dim: states[key][dim] for dim in ('retrieval', 'evidence', 'judgment')},
            'group': classify(states[key], detail, case, turns), 'gold': case['expected']['judgment'],
            'required_quotes': case['expected']['evidence']['required_quotes'], 'turns': turns,
            'delivered': actual.get('judgment') if actual else None, 'origin': detail.get('judgment_origin'),
            'stop': result.get('stop_reason') or detail.get('missing_reason'),
            'search_candidate_counts': [item['candidates'] for item in result.get('coverage', []) if item['tool'] == 'search']})
    return {'suite_sha256': suite.suite_hash, 'report_sha256': hashlib.sha256((root / 'report.json').read_bytes()).hexdigest(),
        'trace_sha256': trace_hash.hexdigest(), 'model': report['configuration']['model'],
        'counts': dict(Counter(case['group'] for case in cases)), 'cases': cases}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    args = parser.parse_args()
    result = json.dumps(diagnose(load_suite(args.suite), args.campaign), ensure_ascii=False, indent=2)
    sys.stdout.buffer.write(result.encode('utf-8') + b'\n')
