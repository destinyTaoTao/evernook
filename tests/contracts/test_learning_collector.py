from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evernook.learning_evaluation import load_suite
from probes.admission.learning import collect, previous_processor
from evernook import MemoryFailure, initialize
from evernook.automatic_preferences import PreferenceUpdates
from evernook import MemoryRuntime
from evernook import paths as runtime_paths


class LearningCollectorTests(unittest.TestCase):
    def test_previous_core_requires_external_absolute_path_without_logging_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-processor-transition-') as temporary:
            root = Path(temporary)
            config, access = initialize(root / 'runtime', client_id='synthetic', entry='fixture')
            processor = PreferenceUpdates(MemoryRuntime(config)).settings(access).rules_processor
            protected_root = Path(runtime_paths.__file__).resolve().parents[2]
            cases = ((Path('synthetic-forbidden-python.exe'), 'absolute_runtime_path_required'),
                     (protected_root / 'synthetic-forbidden-python.exe', 'runtime_inside_checkout'))
            for index, (executable, code) in enumerate(cases):
                output = root / ('previous-' + str(index))
                with self.subTest(code=code), self.assertRaisesRegex(MemoryFailure, code):
                    previous_processor(executable, config, access, processor, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_restore_and_purge_use_real_protocols_and_preserve_missing_judgments(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        selected = replace(suite, steps=tuple(step for step in suite.steps if step['scenario'] in ('control', 'purge')))
        with tempfile.TemporaryDirectory(prefix='evernook-learning-lifecycle-') as temporary:
            root = Path(temporary) / 'collection'
            with patch('probes.admission.learning.load_suite', return_value=selected):
                observations = collect(suite.root, root, full_lifecycle=True)
            rows = {row['case_id']: row for row in observations['rows']}
            self.assertIn('control-restored', rows)
            self.assertIn('purge-continued', rows)
            self.assertTrue(all('judgment' not in row['payload'] for row in rows.values()))
            details = {row['case_id']: row for row in json.loads((root / 'details.json').read_text(encoding='utf8'))}
            self.assertEqual(details['control-restored']['lifecycle']['operation'], 'control_replayed_runtime')
            self.assertEqual(details['purge-continued']['controls'][0]['state'], 'completed')
            control = rows['control-restored']['payload']['retrieval']['source_ids']
            self.assertNotIn('control-source', control)
            purge = rows['purge-continued']['payload']['retrieval']['source_ids']
            self.assertNotIn('purge-source', purge)
            self.assertTrue((root / 'control-restored-runtime' / 'runtime.json').exists())

    def test_small_default_keeps_lifecycle_gaps_explicit(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        selected = replace(suite, steps=tuple(step for step in suite.steps if step['scenario'] in ('control', 'purge')))
        with tempfile.TemporaryDirectory(prefix='evernook-learning-default-') as temporary:
            root = Path(temporary) / 'collection'
            with patch('probes.admission.learning.load_suite', return_value=selected):
                observations = collect(suite.root, root)
            self.assertNotIn('control-restored', {row['case_id'] for row in observations['rows']})
            self.assertNotIn('purge-continued', {row['case_id'] for row in observations['rows']})


if __name__ == '__main__':
    unittest.main()
