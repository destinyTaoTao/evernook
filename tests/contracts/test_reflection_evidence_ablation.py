from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from evernook.learning_evaluation import load_suite
from evernook.reflection_protocol import build_messages
from probes.admission.reflection_evidence_ablation import compare_vectors, paired_packets


class EvidenceAblationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.campaign = Path(self.temp.name).resolve()
        self.suite = load_suite(Path(__file__).resolve().parents[2] / 'eval/zh-learning-v1')
        self.details: dict[str, dict[str, Any]] = {}
        for scenario in ('old', 'style'):
            path = self.campaign / scenario
            path.mkdir()
            db = sqlite3.connect(path / 'memory.sqlite3')
            try:
                db.execute('CREATE TABLE messages(id TEXT PRIMARY KEY,event TEXT,receipt TEXT)')
                for step in self.suite.steps:
                    if step['scenario'] != scenario:
                        continue
                    receipts = []
                    for alias in step['add'] + step['replay']:
                        source = self.suite.sources[alias]
                        receipt = {'message_id': 'actual-' + alias, 'representation': 'original', 'original_saved': True,
                                   'content_version': 1}
                        event = {'source': {'entry': 'fixture', 'branch': 'main', 'session': source['session'], 'event': alias},
                                 'role': source['role'], 'text': source['text'], 'occurred_at': source['at']}
                        if alias in step['add']:
                            db.execute('INSERT INTO messages VALUES(?,?,?)', (receipt['message_id'], json.dumps(event), json.dumps(receipt)))
                        receipts.append({'source_id': alias, 'receipt': receipt, 'replayed_same_id': alias in step['replay']})
                    self.details[step['id']] = {'receipts': receipts}
                db.commit()
            finally:
                db.close()

    def request(self, case_id: str, evidence: list[dict[str, str]]) -> None:
        path = self.campaign / (case_id + '-workers') / 'turn-1'
        path.mkdir(parents=True)
        model_input = {'evidence': evidence, 'question': self.suite.cases[case_id]['question'], 'allowed_view_ids': []}
        (path / 'stdin.bin').write_text(json.dumps({'input_tokens': 4096, 'output_tokens': 512,
            'messages': build_messages(model_input)}), encoding='utf-8')

    def test_preserves_prior_turn_quote_and_slot_adds_only_missing_source_verbatim(self) -> None:
        source = self.suite.sources['style']
        present = {'evidence_id': 'e1', 'message_id': 'actual-style', 'role': source['role'],
                   'quote': source['text'][:-1], 'occurred_at': source['at']}
        self.request('style-continued', [present])
        # Packet construction has no dependency on gold labels or the judge.
        self.suite.cases['style-continued'].pop('expected')
        original, extended, aliases, mapping, _ = paired_packets(self.suite, 'style-continued', self.details, self.campaign)
        self.assertEqual(aliases, {'e1': 'style'})
        self.assertEqual(mapping, {'e1': 'style', 'e2': 'exception'})
        self.assertEqual(original['evidence'], [present])
        missing = self.suite.sources['exception']
        self.assertEqual(extended, {**original, 'evidence': [present, {'evidence_id': 'e2', 'message_id': 'actual-exception',
            'quote': missing['text'], 'role': missing['role'], 'occurred_at': missing['at']}]})
        self.assertEqual(build_messages(original)[:-1], build_messages(extended)[:-1])

    def test_replays_are_one_original_and_supply_no_count_or_answer(self) -> None:
        self.request('old-continued', [])
        source = self.suite.sources['old']
        original, extended, _, mapping, _ = paired_packets(self.suite, 'old-continued', self.details, self.campaign)
        self.assertEqual(mapping, {'e1': 'old'})
        self.assertEqual(extended, {**original, 'evidence': [{'evidence_id': 'e1', 'message_id': 'actual-old',
            'quote': source['text'], 'role': source['role'], 'occurred_at': source['at']}]})
        self.details['old-replay']['receipts'][0]['receipt']['message_id'] = 'wrong-id'
        with self.assertRaisesRegex(ValueError, 'inconsistent_replayed_receipt'):
            paired_packets(self.suite, 'old-continued', self.details, self.campaign)

    def test_source_link_is_verified_in_its_own_scenario_ledger(self) -> None:
        self.request('style-continued', [])
        self.details['style-initial']['receipts'][0]['receipt']['message_id'] = 'actual-exception'
        with self.assertRaisesRegex(ValueError, 'stored_source_mismatch'):
            paired_packets(self.suite, 'style-continued', self.details, self.campaign)

    def test_cosine_uses_frozen_search_identity_and_the_stored_vectors(self) -> None:
        case = self.suite.cases['style-continued']
        self.request(case['id'], [])
        digest = hashlib.sha256(case['question'].encode()).hexdigest()
        search = {'tool': 'search', 'query_sha256': digest, 'query_chars': len(case['question']),
                  'semantic': {'state': 'ready', 'space_id': 'test-space'}, 'candidates': 1}
        self.details[case['id']]['result'] = {'coverage': [search]}
        db = sqlite3.connect(self.campaign / 'style' / 'memory.sqlite3')
        try:
            db.execute('CREATE TABLE semantic_chunks_v1(space_id,message_id,start,end,state_version,content_version,vector)')
            for alias, values in [('style', (1., 0.)), ('exception', (0., 1.))]:
                db.execute('INSERT INTO semantic_chunks_v1 VALUES(?,?,0,?,1,1,?)', ('test-space',
                    'actual-' + alias, len(self.suite.sources[alias]['text']), struct.pack('<2f', *values)))
            db.commit()
        finally:
            db.close()
        with patch('probes.admission.reflection_evidence_ablation.PANEL', (case['id'],)), \
                patch('probes.admission.reflection_evidence_ablation.LocalMiniLM.from_file') as loader:
            backend = loader.return_value
            backend.space.key = 'test-space'
            backend.space.dimensions = 2
            backend.embed.return_value = ((1., 0.),)
            results = compare_vectors(self.suite, self.campaign, self.details, Path('unused'))
            self.assertEqual([(row['source'], row['at_least_0_4']) for row in results[0]['source_scores']],
                             [('style', True), ('exception', False)])
            search['query_sha256'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'search_query_or_embedding_space_mismatch'):
                compare_vectors(self.suite, self.campaign, self.details, Path('unused'))
            backend.embed.assert_called_once_with((case['question'],), timeout=30)
