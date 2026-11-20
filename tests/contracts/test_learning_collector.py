from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook.learning_evaluation import load_suite
from probes.admission import learning, observations, views
from probes.admission.learning import collect, previous_processor
from evernook import MemoryFailure, initialize
from evernook.automatic_preferences import PreferenceUpdates
from evernook import MemoryRuntime, SemanticRecall
from evernook.models import LexicalStatus
from evernook.observation_processing import ObservationProcessing
from evernook.observations import ObservationRegistry
from evernook import paths as runtime_paths
from evernook.semantic import SemanticStatus


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

    def test_derived_collectors_replay_actual_controls_and_keep_unmeasured_dimensions_missing(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        selected = replace(suite, steps=tuple(step for step in suite.steps if step['scenario'] in ('control', 'purge')))
        with tempfile.TemporaryDirectory(prefix='evernook-learning-derived-lifecycle-') as temporary:
            for module, extra in ((observations, ()), (views, (None,))):
                arm = module.__name__.rsplit('.', 1)[-1]
                with self.subTest(arm=arm):
                    root = Path(temporary) / arm
                    with patch.object(module, 'load_suite', return_value=selected):
                        report = module.collect(suite.root, root, *extra, full_lifecycle=True)
                    details = {row['case_id']: row for row in json.loads((root / 'details.json').read_text(encoding='utf8'))}
                    payloads = {row['case_id']: row['payload'] for row in
                        json.loads((root / 'observations.json').read_text(encoding='utf8'))['rows']}
                    self.assertEqual(details['control-restored']['lifecycle']['operation'], 'control_replayed_runtime')
                    self.assertEqual(details['control-continued']['controls'][0]['status'], 'suppressed')
                    self.assertEqual(details['purge-continued']['controls'][0]['state'], 'completed')
                    self.assertEqual(report['arms'][arm]['judgment']['missing'], 33)
                    self.assertEqual(report['arms'][arm]['host_answer']['missing'], 33)
                    if arm == 'observations':
                        # These originals have no eligible finite preference candidate.
                        self.assertEqual(payloads['control-initial']['retrieval']['source_ids'], [])
                        self.assertEqual(payloads['purge-initial']['retrieval']['source_ids'], [])
                        self.assertTrue(all(receipt['published'] == 0 for detail in details.values()
                            for receipt in detail['maintenance_receipts']))
                    else:
                        self.assertIn('suppressed', payloads['control-initial']['retrieval']['source_ids'])
                        self.assertIn('purge-source', payloads['purge-initial']['retrieval']['source_ids'])
                        self.assertNotIn('suppressed', payloads['control-restored']['retrieval']['source_ids'])
                        self.assertNotIn('purge-source', payloads['purge-continued']['retrieval']['source_ids'])
                        self.assertEqual(details['control-continued']['view_before_definition']['state'], 'needs_refresh')
                        self.assertIsNone(details['control-continued']['view_before_definition']['data'])

    def test_observation_operational_failure_leaves_case_missing_and_preserves_restore(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        control = replace(suite, steps=tuple(step for step in suite.steps if step['scenario'] == 'control'))
        operations = ((ObservationProcessing, 'tick', 'observation_timeout', 1),
                      (ObservationRegistry, 'related', 'storage_busy', 1),
                      (ObservationProcessing, 'status', 'storage_busy', 2))
        with tempfile.TemporaryDirectory(prefix='evernook-learning-observation-failure-') as temporary:
            for cls, operation, code, fail_at in operations:
                run = getattr(cls, operation)
                calls = 0

                def fail_once(*args: Any, **kwargs: Any) -> Any:
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise MemoryFailure(code)
                    return run(*args, **kwargs)

                with self.subTest(operation=operation):
                    root = Path(temporary) / operation
                    with patch.object(observations, 'load_suite', return_value=control), patch.object(
                            cls, operation, autospec=True, side_effect=fail_once):
                        observations.collect(suite.root, root, full_lifecycle=True)
                    details = {row['case_id']: row for row in json.loads((root / 'details.json').read_text(encoding='utf8'))}
                    captured = json.loads((root / 'observations.json').read_text(encoding='utf8'))
                    missing = details['control-initial']
                    self.assertEqual(missing['error_stage'], operation)
                    self.assertEqual(missing['error_code'], code)
                    self.assertEqual(missing['missing_reason'], 'observation_collection_failed')
                    self.assertTrue(missing['receipts'])
                    if operation == 'status':
                        self.assertIn('candidate_page', missing)
                        self.assertTrue(missing['maintenance_receipts'])
                    self.assertEqual({row['case_id'] for row in captured['rows']}, {'control-continued', 'control-restored'})
                    self.assertTrue((root / 'control-old-archive').exists())
                    self.assertEqual(details['control-restored']['lifecycle']['operation'], 'control_replayed_runtime')

    def test_observation_integrity_or_authorization_failure_remains_fatal(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        one = replace(suite, steps=(suite.steps[0],))
        with tempfile.TemporaryDirectory(prefix='evernook-learning-observation-fatal-') as temporary:
            for code in ('observation_run_incomplete', 'permission_denied', 'incompatible_contract', 'storage_unavailable'):
                with self.subTest(code=code), patch.object(observations, 'load_suite', return_value=one), patch.object(
                        ObservationRegistry, 'related', side_effect=MemoryFailure(code)), self.assertRaisesRegex(MemoryFailure, code):
                    observations.collect(suite.root, Path(temporary) / code)

    def test_view_index_timeout_keeps_restore_and_unmeasured_case_missing(self) -> None:
        suite = load_suite(Path('eval/zh-learning-v1'))
        control = replace(suite, steps=tuple(step for step in suite.steps if step['scenario'] == 'control'))
        run = learning.prepare_indexes
        failed = False

        def timeout_once(*args: Any, **kwargs: Any) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise MemoryFailure('semantic_refresh_timeout')
            run(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix='evernook-views-index-timeout-') as temporary:
            root = Path(temporary) / 'views'
            with patch.object(views, 'load_suite', return_value=control), patch.object(
                    learning, 'prepare_indexes', side_effect=timeout_once):
                views.collect(suite.root, root, None, full_lifecycle=True)
            details = {row['case_id']: row for row in json.loads((root / 'details.json').read_text(encoding='utf8'))}
            records = json.loads((root / 'observations.json').read_text(encoding='utf8'))
            self.assertEqual(details['control-initial']['error_stage'], 'prepare_indexes')
            self.assertEqual(details['control-initial']['error_code'], 'semantic_refresh_timeout')
            self.assertNotIn('view', details['control-initial'])
            self.assertEqual({row['case_id'] for row in records['rows']}, {'control-continued', 'control-restored'})
            self.assertEqual(details['control-restored']['lifecycle']['operation'], 'control_replayed_runtime')

    def test_explicitly_unavailable_index_never_loops_or_counts_as_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-learning-index-unavailable-') as temporary:
            config, access = initialize(Path(temporary) / 'runtime', client_id='synthetic', entry='fixture')
            runtime = MemoryRuntime(config)
            fixture = learning.ScenarioFixture(config, runtime, access)
            recall = SemanticRecall(runtime)
            behind = LexicalStatus('behind', 1, 0, 1, None, None)
            detail: dict[str, Any] = {}
            with patch.object(runtime, 'lexical_status', side_effect=(behind,)), patch.object(
                    runtime, 'refresh_lexical', return_value=replace(behind, state='unavailable')) as refresh:
                self.assertFalse(learning.prepare_case_indexes(fixture, recall, detail, embed=False, source_count=1))
            self.assertEqual(detail['error_code'], 'lexical_index_unavailable')
            refresh.assert_called_once_with(access)

            semantic = SemanticStatus('unavailable', 'fixture-space', None, None, None, None, 'storage_busy')
            with patch.object(recall, 'status', return_value=semantic):
                self.assertFalse(learning.prepare_case_indexes(fixture, recall, detail, embed=True, source_count=1))
            self.assertEqual(detail['error_code'], 'semantic_index_unavailable')


if __name__ == '__main__':
    unittest.main()
