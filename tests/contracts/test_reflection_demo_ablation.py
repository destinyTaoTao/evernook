from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evernook.learning_evaluation import load_suite
from evernook.reflection_protocol import build_messages
from probes.admission.reflection_demo_ablation import packet


class ReplayPacketTests(unittest.TestCase):
    def test_replays_only_the_original_question_and_sent_evidence(self) -> None:
        suite = load_suite(Path(__file__).resolve().parents[2] / 'eval/zh-learning-v1')
        case = suite.cases['multi-initial']
        source_id = case['add'][0]
        source = suite.sources[source_id]
        evidence = {'evidence_id': 'e1', 'message_id': 'synthetic-first', 'quote': source['text'],
                    'role': source['role'], 'occurred_at': source['at']}
        payload = {'question': case['question'], 'evidence': [evidence], 'allowed_view_ids': []}
        detail = {'receipts': [{'source_id': source_id, 'receipt': {'message_id': 'synthetic-first'}}]}
        messages = build_messages(payload)
        request = {'input_tokens': 4096, 'output_tokens': 512, 'messages': messages}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wire.bin'
            path.write_text(json.dumps(request), encoding='utf-8')
            original, aliases, _ = packet(suite, case['id'], detail, path)
            self.assertEqual(original, payload)
            self.assertEqual(aliases, {'e1': source_id})
            messages[1]['content'] = 'changed example'
            path.write_text(json.dumps(request), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'replay_messages_changed'):
                packet(suite, case['id'], detail, path)
