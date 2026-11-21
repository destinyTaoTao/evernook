"""Replay actual first-turn evidence with and without the three protocol demonstrations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from evernook import MemoryFailure
from evernook.learning_evaluation import LearningSuite, load_suite
from evernook.paths import external
from evernook.reflection_model import LocalReflectionModel
from evernook.reflection_protocol import build_messages, translate
from probes.admission.learning import save
from probes.admission.semantic_reflection import judge_generated


# Fixed before generation. Includes three previously passing controls.
PANEL = ('long-initial', 'authority-initial', 'multi-initial', 'negation-initial',
         'control-initial', 'change-initial', 'style-initial', 'insufficient-initial')


def packet(suite: LearningSuite, case_id: str, detail: dict[str, Any], path: Path
           ) -> tuple[dict[str, Any], dict[str, str], str]:
    wire = path.read_bytes()
    request = json.loads(wire)
    actual = json.loads(request['messages'][-1]['content'])
    case = suite.cases[case_id]
    if (actual['question'] != case['question'] or actual['allowed_view_ids']
            or (request['input_tokens'], request['output_tokens']) != (4096, 512)):
        raise ValueError('unexpected_replay_request')
    receipts = {r['receipt']['message_id']: r['source_id'] for r in detail['receipts']}
    evidence, aliases = [], {}
    for ref, source in actual['evidence'].items():
        alias = receipts[source['message_id']]
        original = suite.sources[alias]
        if (alias not in case['available_sources'] or not source['quote'] or source['quote'] not in original['text']
                or (source['role'], source['statement_at']) != (original['role'], original['at'])):
            raise ValueError('replay_source_mismatch')
        aliases[ref] = alias
        evidence.append({'evidence_id': ref, 'message_id': source['message_id'], 'quote': source['quote'],
                         'role': source['role'], 'occurred_at': source['statement_at']})
    payload = {'question': actual['question'], 'evidence': evidence, 'allowed_view_ids': []}
    if build_messages(payload) != request['messages']:
        raise ValueError('replay_messages_changed')
    return payload, aliases, hashlib.sha256(wire).hexdigest()


def trial(model: LocalReflectionModel, payload: dict[str, Any], aliases: dict[str, str],
          expected: dict[str, Any], omit: bool) -> dict[str, Any]:
    messages = build_messages(payload)
    selected = [messages[0], messages[-1]] if omit else messages
    output: list[object] = []

    def capture(action: object, context: dict[str, Any]) -> dict[str, Any]:
        output.append(action)
        return translate(action, context)

    row: dict[str, Any] = {'condition': 'no_demos' if omit else 'baseline',
        'messages_sha256': hashlib.sha256(json.dumps(selected, ensure_ascii=False).encode()).hexdigest()}
    try:
        with patch('evernook.reflection_model.build_messages', return_value=selected), \
                patch('evernook.reflection_model.translate', side_effect=capture):
            turn = model.turn(payload, input_tokens=4096, output_tokens=512, timeout=30, cancel=None)
        row.update(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens, action=turn.action['action'])
        row['raw_action'] = output[0] if output else None
        row['judgment'] = judge_generated(expected, json.dumps(row['raw_action'], ensure_ascii=False), payload, aliases)
    except MemoryFailure as error:
        row.update(error=error.code, judgment={'state': 'unobserved'})
    return row


def run(campaign: Path, config: Path, destination: Path) -> None:
    campaign, destination = external(campaign), external(destination)
    suite = load_suite(Path(__file__).resolve().parents[2] / 'eval/zh-learning-v1')
    details = {item['case_id']: item for item in json.loads((campaign / 'details.json').read_bytes())}
    options = json.loads(external(config).read_bytes())
    model = LocalReflectionModel(node=Path(options['node']), transformers_dir=Path(options['transformers_dir']),
                                 model_dir=Path(options['model_dir']))
    prior = json.loads((campaign / 'observations.json').read_bytes())
    if prior['suite_hash'] != suite.suite_hash or prior['configuration']['model'] != model.processor_version:
        raise ValueError('replay_identity_mismatch')
    destination.mkdir()
    rows: list[dict[str, Any]] = []
    for number, case_id in enumerate(PANEL):
        payload, aliases, fingerprint = packet(suite, case_id, details[case_id],
            campaign / (case_id + '-workers') / 'turn-1' / 'stdin.bin')
        expected = suite.cases[case_id]['expected']['judgment']
        for omit in ((False, True) if number % 2 == 0 else (True, False)):
            row = trial(model, payload, aliases, expected, omit)
            rows.append({'case_id': case_id, 'original_request_sha256': fingerprint, **row})
            save(destination / 'rows.json', rows)
            print(case_id, row['condition'], row['judgment']['state'], flush=True)
    save(destination / 'manifest.json', {'suite': suite.suite_hash, 'model': model.processor_version,
        'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'panel': PANEL, 'budget': {'input_tokens': 4096, 'output_tokens': 512, 'timeout_s': 30},
        'scope': 'Synthetic first-turn offline replay. Exact raw judgment only. No retrieval, tool followup or model admission.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--model-config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run(args.campaign, args.model_config, args.output)
