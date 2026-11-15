from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from probes.admission.scale_receipt import main


class ScaleReceiptProbeTests(unittest.TestCase):
    def _receipt(self, *, correct_original: bool, writer_actual: int = 1) -> tuple[int, dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix='evernook-scale-receipt-') as temporary:
            root = Path(temporary)
            fixture = root / 'fixture'; fixture.mkdir()
            config, access = initialize(fixture / 'runtime', client_id='synthetic', entry='synthetic-scale')
            pair_client(config, client_id=access.client_id, entry='synthetic-scale',
                        access_path=config.parent / 'mcp-access-scale-writer.json')
            raw = Path('probes/admission/policy.json').read_bytes()
            policy = json.loads(raw)
            MemoryRuntime(config).submit(access, Event(Source('synthetic-scale', 'zh-scale-v1', 'main', 'm0000000'),
                'user', 0, '2026-11-03T00:00:00+00:00', 'complete',
                policy['scale']['pure_semantic_original'] if correct_original else '合成原文不匹配'))
            (fixture / 'manifest.json').write_text(json.dumps({'fixture_version': policy['scale']['fixture_version'],
                'policy_sha256': hashlib.sha256(raw).hexdigest(), 'expected_messages': 1}), encoding='utf8')
            (fixture / 'ingest-report.json').write_text(json.dumps({'completed': 1, 'actual_messages': writer_actual,
                'expected_messages': 1, 'error_code': None}), encoding='utf8')
            embedding = root / 'embedding.json'
            embedding.write_text(json.dumps({'schema_version': 1, 'node': sys.executable,
                'transformers_dir': str(root), 'model_dir': str(root)}), encoding='utf8')
            output = root / 'receipt'
            with patch.object(sys, 'argv', ['receipt', '--fixture', str(fixture), '--embedding-config', str(embedding),
                                           '--output', str(output)]):
                code = main()
            report = json.loads((output / 'report.json').read_bytes())
            return code, report

    def test_original_receipt_success_does_not_imply_full_index_readiness(self) -> None:
        code, report = self._receipt(correct_original=True)
        self.assertEqual(code, 0)
        self.assertTrue(report['complete_fixture'])
        self.assertFalse(report['ready_for_full_scale_query_matrix'])
        self.assertTrue(report['ready_for_target_size_measurements'])

    def test_count_match_cannot_publish_ready_when_original_roundtrip_failed(self) -> None:
        code, report = self._receipt(correct_original=False)
        self.assertEqual(code, 2)
        self.assertFalse(report['ready_for_target_size_measurements'])
        self.assertFalse(report['ready_for_full_scale_query_matrix'])
        self.assertFalse(report['complete_fixture'])
        self.assertEqual(report['admission'], 'blocked')

    def test_writer_actual_count_disagreement_blocks_readiness(self) -> None:
        code, report = self._receipt(correct_original=True, writer_actual=2)
        self.assertEqual(code, 2)
        self.assertFalse(report['counts_complete'])
        self.assertFalse(report['writer_report_count_matches'])
        self.assertFalse(report['ready_for_target_size_measurements'])
