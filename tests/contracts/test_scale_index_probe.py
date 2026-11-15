from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import sys
import threading

from evernook import EmbeddingSpace, Event, MemoryRuntime, Source, initialize
from probes.admission.scale_index import main


class OverflowEmbedding:
    space = EmbeddingSpace('synthetic-overflow', 'v1', 3, 'synthetic', 64, 16)

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[None, ...]:
        return tuple(None for _ in texts)


class ScaleIndexProbeTests(unittest.TestCase):
    def test_acknowledged_token_failure_does_not_pass_complete_vector_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-scale-gate-') as temporary:
            root = Path(temporary)
            fixture = root / 'fixture'
            fixture.mkdir()
            config, access = initialize(fixture / 'runtime', client_id='synthetic', entry='fixture')
            MemoryRuntime(config).submit(access, Event(Source('fixture', 'synthetic', 'main', 'one'),
                'user', 1, None, 'complete', '合成模型溢出'))
            output = root / 'result'
            with patch.object(sys, 'argv', ['scale_index', '--fixture', str(fixture), '--output', str(output),
                    '--embedding-config', str(root / 'not-used')]), patch(
                    'probes.admission.scale_index.LocalMiniLM.from_file', return_value=OverflowEmbedding()):
                self.assertEqual(main(), 2)
            report = json.loads((output / 'report.json').read_text(encoding='utf8'))
            self.assertEqual(report['semantic']['state'], 'ready')
            self.assertEqual(report['semantic']['failed_chunks'], 1)
            self.assertFalse(report['complete_vector_coverage'])
            self.assertEqual(report['admission'], 'blocked')
