"""Replay frozen first-turn model input, changing only which scenario originals are present."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from evernook.embedding import LocalMiniLM, unit_vector
from evernook.learning_evaluation import LearningSuite, load_suite
from evernook.paths import external
from evernook.reflection_model import LocalReflectionModel
from evernook.vector_distance import distance_function
from probes.admission.learning import save
from probes.admission.reflection_demo_ablation import packet, trial


PANEL = ('old-initial', 'old-replay', 'old-continued', 'style-continued')


def scenario_receipts(suite: LearningSuite, case_id: str, details: dict[str, dict[str, Any]],
                      db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    case = suite.cases[case_id]
    receipts: dict[str, dict[str, Any]] = {}
    for step in suite.steps:
        if step['scenario'] != case['scenario']:
            continue
        rows = details[step['id']].get('receipts', [])
        if [row['source_id'] for row in rows] != step['add'] + step['replay']:
            raise ValueError('unexpected_scenario_receipts')
        for row in rows:
            alias, receipt = row['source_id'], row['receipt']
            if alias in step['replay'] and (row.get('replayed_same_id') is not True or receipts.get(alias) != receipt):
                raise ValueError('inconsistent_replayed_receipt')
            receipts[alias] = receipt
        if step['id'] == case_id:
            break
    if set(receipts) != set(case['available_sources']):
        raise ValueError('incomplete_scenario_receipts')
    for alias, receipt in receipts.items():
        record = db.execute('SELECT event,receipt FROM messages WHERE id=?', (receipt['message_id'],)).fetchone()
        source = suite.sources[alias]
        if not record or json.loads(record[1]) != receipt or not receipt['original_saved'] or receipt['representation'] != 'original':
            raise ValueError('stored_receipt_mismatch')
        event = json.loads(record[0])
        if (event['source'] != {'entry': 'fixture', 'session': source['session'], 'branch': 'main', 'event': alias}
                or (event['text'], event['role'], event['occurred_at']) != (source['text'], source['role'], source['at'])):
            raise ValueError('stored_source_mismatch')
    return receipts


def paired_packets(suite: LearningSuite, case_id: str, details: dict[str, dict[str, Any]], campaign: Path
                   ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str], str]:
    scenario = suite.cases[case_id]['scenario']
    with closing(sqlite3.connect((campaign / scenario / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        receipts = scenario_receipts(suite, case_id, details, db)
    detail = {'receipts': [{'source_id': alias, 'receipt': receipt} for alias, receipt in receipts.items()]}
    original, aliases, digest = packet(suite, case_id, detail, campaign / (case_id + '-workers') / 'turn-1/stdin.bin')
    extended_aliases = dict(aliases)
    evidence = list(original['evidence'])
    for alias, receipt in receipts.items():
        if alias in aliases.values():
            continue
        number = len(evidence) + 1
        while f'e{number}' in extended_aliases:
            number += 1
        ref = f'e{number}'
        source = suite.sources[alias]
        evidence.append({'evidence_id': ref, 'message_id': receipt['message_id'], 'quote': source['text'],
                         'role': source['role'], 'occurred_at': source['at']})
        extended_aliases[ref] = alias
    return original, {**original, 'evidence': evidence}, aliases, extended_aliases, digest


def compare_vectors(suite: LearningSuite, campaign: Path, details: dict[str, dict[str, Any]],
                    config: Path) -> list[dict[str, Any]]:
    backend = LocalMiniLM.from_file(config)
    results = []
    for case_id in PANEL:
        case = suite.cases[case_id]
        question = case['question']
        searches = [r for r in details[case_id]['result']['coverage'] if r['tool'] == 'search']
        digest = hashlib.sha256(question.encode()).hexdigest()
        if (len(searches) != 1 or searches[0]['query_sha256'] != digest or searches[0]['query_chars'] != len(question)
                or searches[0]['semantic']['space_id'] != backend.space.key or searches[0]['semantic']['state'] != 'ready'):
            raise ValueError('search_query_or_embedding_space_mismatch')
        raw = backend.embed((question,), timeout=30)[0]
        if raw is None:
            raise ValueError('query_vector_missing')
        vector = unit_vector(raw, backend.space.dimensions)
        _, _, sent, _, _ = paired_packets(suite, case_id, details, campaign)
        scores = []
        with closing(sqlite3.connect((campaign / case['scenario'] / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            receipts = scenario_receipts(suite, case_id, details, db)
            function, encoded, engine = distance_function(db, vector)
            for alias, receipt in receipts.items():
                chunks = db.execute(f"""SELECT start,end,{function}(vector,?) FROM semantic_chunks_v1
                    WHERE space_id=? AND message_id=? AND content_version=? AND state_version=1 ORDER BY start""",
                    (encoded, backend.space.key, receipt['message_id'], receipt['content_version'])).fetchall()
                if len(chunks) != 1 or chunks[0][:2] != (0, len(suite.sources[alias]['text'])):
                    raise ValueError('expected_one_current_full_source_vector')
                score = 1.0 - chunks[0][2]
                scores.append({'source': alias, 'message_id': receipt['message_id'], 'cosine': score,
                               'at_least_0_4': score >= 0.4, 'sent_in_original': alias in sent.values()})
        results.append({'case_id': case_id, 'query_sha256': digest, 'space': backend.space.key, 'engine': engine,
                        'actual_candidates': searches[0]['candidates'], 'source_scores': scores})
    return results


def run(campaign: Path, config: Path, destination: Path, embedding_config: Path | None = None) -> None:
    campaign, config, destination = external(campaign), external(config), external(destination)
    suite = load_suite(Path(__file__).resolve().parents[2] / 'eval/zh-learning-v1')
    details = {row['case_id']: row for row in json.loads((campaign / 'details.json').read_bytes())}
    config_bytes = config.read_bytes()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    options = json.loads(config_bytes)
    model = LocalReflectionModel(node=Path(options['node']), transformers_dir=Path(options['transformers_dir']),
                                 model_dir=Path(options['model_dir']))
    prior = json.loads((campaign / 'observations.json').read_bytes())
    scope = json.loads((campaign / 'scope.json').read_bytes())
    if (prior['suite_hash'] != suite.suite_hash or prior['configuration']['model'] != model.processor_version
            or scope['model_config_sha256'] != config_hash):
        raise ValueError('replay_identity_mismatch')
    destination.mkdir()
    save(destination / 'manifest.json', {'suite': suite.suite_hash, 'model': model.processor_version,
        'model_config_sha256': config_hash, 'panel': PANEL, 'budget': {'input_tokens': 4096, 'output_tokens': 512, 'timeout_s': 30},
        'implementation_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in
            [Path(__file__), Path(__file__).with_name('reflection_demo_ablation.py')]},
        'scope': 'Offline first-turn replay only. No retrieval, corpus-size statement, tool replies or additional instructions.'})
    rows: list[dict[str, Any]] = []
    for number, case_id in enumerate(PANEL):
        original, extended, aliases, extended_aliases, digest = paired_packets(suite, case_id, details, campaign)
        archived = (campaign / (case_id + '-workers') / 'turn-1/stdout.bin').read_bytes()
        wire = json.loads(archived)
        if wire != details[case_id]['model_wire'][0] or wire.get('status') != 'ok' or wire.get('network_attempts') != 0:
            raise ValueError('unexpected_archived_worker_result')
        prior_action = json.loads(wire['action_json'])
        arms = [('as_sent', original, aliases), ('missing_originals_added', extended, extended_aliases)]
        for name, payload, mapping in (arms if number % 2 == 0 else reversed(arms)):
            row = trial(model, payload, mapping, suite.cases[case_id]['expected']['judgment'], omit=False)
            rows.append({**row, 'case_id': case_id, 'condition': name, 'original_request_sha256': digest,
                         'original_response_sha256': hashlib.sha256(archived).hexdigest(), 'archived_raw_action': prior_action,
                         'evidence': payload['evidence'], 'aliases': mapping})
            save(destination / 'rows.json', rows)
            print(case_id, name, row['judgment']['state'], flush=True)
    if embedding_config is not None:
        embedding_config = external(embedding_config)
        save(destination / 'cosine.json', {'embedding_config_sha256': hashlib.sha256(embedding_config.read_bytes()).hexdigest(),
            'cases': compare_vectors(suite, campaign, details, embedding_config),
            'scope': 'Independent comparison to the default 0.4 threshold. Source vectors already exist in the frozen scenario ledger.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--embedding-config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.campaign, args.model_config, args.output, args.embedding_config)
