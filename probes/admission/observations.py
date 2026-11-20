"""Frozen-suite audit of review-only finite observations, not normal-context or model admission."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
import time
from typing import Any

from evernook.learning_evaluation import load_suite, score
from evernook.models import MemoryFailure
from evernook.observations import ObservationRegistry
from evernook.observation_processing import ObservationProcessing
from evernook.paths import external

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from probes.admission.learning import LearningFixtures, implementation_identity, save


MAX_FIXTURE_SOURCES = 64
OPERATIONAL_FAILURES = frozenset({'observation_timeout', 'observation_canceled', 'observation_paused',
    'candidate_run_busy', 'candidate_lease_expired', 'storage_busy', 'observation_fixture_did_not_converge',
    'observation_processing_disabled'})


def collect(suite_root: Path, output: Path, *, full_lifecycle: bool = False) -> dict[str, Any]:
    suite = load_suite(suite_root)
    root = external(output)
    root.mkdir()
    fixtures = LearningFixtures(suite, root, full_lifecycle=full_lifecycle)
    workers: dict[str, ObservationProcessing] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    implementation = implementation_identity(Path(__file__))
    configuration = {'model': 'none; finite preference observation candidates', 'processor': 'uninitialized',
        'budget': {'candidate_k': 10, 'related_observations': 16, 'batch_sources': 16, 'batch_candidates': 32, 'batch_seconds': 5,
                   'max_fixture_sources': MAX_FIXTURE_SOURCES, 'full_lifecycle': full_lifecycle,
                   'strategy': 'maintenance candidate audit; no observation publication or context injection'},
        'policy_sha256': suite.manifest['policy_sha256'], 'implementation': implementation['sha256']}
    observations = {'suite_hash': suite.suite_hash, 'configuration': configuration, 'rows': rows}
    for step in suite.steps:
        phase = step['phase']
        source_count = len(step['available_sources'])
        detail: dict[str, Any] = {'case_id': step['id'], 'phase': phase, 'corpus_sources': source_count}
        details.append(detail)
        if phase == 'processor_changed':
            detail['missing_reason'] = 'observation_processor_transition_not_collected'
        elif source_count > MAX_FIXTURE_SOURCES:
            detail['missing_reason'] = 'fixture_source_limit'
        elif not full_lifecycle and (phase == 'restored' or any(control['action'] == 'purge' for control in step['controls'])):
            detail['missing_reason'] = 'lifecycle_protocol_not_enabled'
        if 'missing_reason' in detail:
            save(root / 'details.json', details)
            continue
        scenario = step['scenario']
        fixture = fixtures.open(scenario)
        fixtures.apply(fixture, step, detail)
        runtime, access = fixture.runtime, fixture.access
        worker = workers.get(scenario)
        configure = worker is None
        if worker is None or worker.runtime is not runtime:
            worker = ObservationProcessing(runtime)
            if configuration['processor'] == 'uninitialized':
                configuration['processor'] = worker.processor
            if configuration['processor'] != worker.processor:
                raise RuntimeError('observation_processor_changed_during_collection')
        receipts: list[dict[str, Any]] = []
        detail['maintenance_receipts'] = receipts
        stage = 'configure'
        try:
            if configure:
                detail['observation_configure'] = worker.configure(access, enabled=True)
            workers[scenario] = worker
            stage = 'tick'
            for _ in range(128):
                receipt = worker.tick(access, limit=16, timeout=5)
                receipts.append(receipt)
                if receipt['state'] == 'idle':
                    break
                if receipt['state'] == 'disabled':
                    raise MemoryFailure('observation_processing_disabled')
            else:
                raise MemoryFailure('observation_fixture_did_not_converge')
            stage = 'related'
            registry = ObservationRegistry(runtime)
            started = time.perf_counter()
            # No gold topics or expected IDs participate in candidate selection.
            page = registry.related(access, 'query:unspecified-topic', query=step['question'][-256:], limit=16)
            elapsed = (time.perf_counter() - started) * 1000
            detail['candidate_page'] = asdict(page)
            stage = 'status'
            detail['status'] = worker.status(access)
        except MemoryFailure as failure:
            detail.update(error_stage=stage, error_code=failure.code)
            if failure.code not in OPERATIONAL_FAILURES:
                save(root / 'details.json', details)
                raise
            detail['missing_reason'] = 'observation_collection_failed'
        else:
            aliases = {value: key for key, value in fixture.ids.items()}
            selected: list[str] = []
            citations = []
            for view in page.rows:
                for context in view.source_contexts:
                    message_id = context['message_id']
                    if message_id not in selected:
                        if len(selected) >= 10:
                            continue
                        selected.append(message_id)
                    citations.append({'source_id': aliases[message_id], **{key: context[key]
                        for key in ('start', 'end', 'quote', 'state_version', 'content_version')}})
            rows.append({'case_id': step['id'], 'arm': 'observations', 'payload': {
                'retrieval': {'source_ids': [aliases[message_id] for message_id in selected]}, 'evidence': {'citations': citations}},
                'measurements': {'elapsed_ms': elapsed, 'private_bytes': None, 'remaining_children': None}})
            detail['publication'] = 'review-only candidates'
        save(root / 'details.json', details)
        fixtures.snapshot(scenario)
        save(root / 'observations.json', observations)
    save(root / 'observations.json', observations)
    report = score(suite, observations)
    save(root / 'report.json', report)
    save(root / 'scope.json', {'implementation': implementation,
        'scope': 'Finite review-only candidate related-query and provenance audit on the frozen corpus. The actual lifecycle receipts are separate from scoring. Publication eligibility, conclusion support, host outputs and model judgments remain unmeasured.',
        'admission': 'pending'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-lifecycle', action='store_true')
    args = parser.parse_args()
    collect(args.suite, args.output, full_lifecycle=args.full_lifecycle)
