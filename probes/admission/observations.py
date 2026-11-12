"""Frozen-suite audit of review-only finite observations, not normal-context or model admission."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import time
from typing import Any

from evernook import Change, Event, MemoryRuntime, Source, initialize
from evernook.learning_evaluation import load_suite, score
from evernook.observations import ObservationRegistry
from evernook.observation_processing import ObservationProcessing
from evernook.paths import external
from learning import implementation_identity, save


def collect(suite_root: Path, output: Path) -> dict[str, Any]:
    suite = load_suite(suite_root)
    root = external(output)
    root.mkdir()
    runtimes: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    configuration = {'model': 'none; finite preference observation candidates', 'processor': 'uninitialized',
        'budget': {'candidate_k': 10, 'related_observations': 16, 'batch_sources': 16, 'batch_candidates': 32, 'batch_seconds': 5,
                   'strategy': 'maintenance-candidate-audit; not published or injected'},
        'policy_sha256': suite.manifest['policy_sha256'], 'implementation': implementation_identity()['sha256']}
    observations = {'suite_hash': suite.suite_hash, 'configuration': configuration, 'rows': rows}
    for step in suite.steps:
        detail: dict[str, Any] = {'case_id': step['id'], 'phase': step['phase']}
        details.append(detail)
        if (step['phase'] in {'restored', 'processor_changed'} or len(step['available_sources']) > 64
                or any(control['action'] == 'purge' for control in step['controls'])):
            detail['missing_reason'] = 'restore_processor_or_scale_purge_protocol_not_collected'
            continue
        scenario = step['scenario']
        if scenario not in runtimes:
            config, access = initialize(root / scenario, client_id='synthetic', entry='fixture')
            runtime = MemoryRuntime(config)
            worker = ObservationProcessing(runtime)
            worker.configure(access, enabled=True)
            if not runtimes:
                configuration['processor'] = worker.processor
            if configuration['processor'] != worker.processor:
                raise RuntimeError('observation_processor_changed_during_collection')
            runtimes[scenario] = runtime, access, worker, {}, {}
        runtime, access, worker, ids, events = runtimes[scenario]
        for alias in step['add']:
            source = suite.sources[alias]
            event = Event(Source('fixture', source['session'], 'main', alias), source['role'], len(events), source['at'], 'complete', source['text'])
            receipt = runtime.submit(access, event)
            ids[alias], events[alias] = receipt.message_id, event
        for alias in step['replay']:
            assert runtime.submit(access, events[alias]).message_id == ids[alias]
        for control in step['controls']:
            state = runtime.state(access, ids[control['source_id']])
            runtime.change(access, Change('control-' + step['id'], ids[control['source_id']], state.version,
                control['action'], ids[control['evidence_id']], 'synthetic fixture control'))
        receipts = []
        for _ in range(128):
            receipt = worker.tick(access, limit=16, timeout=5)
            receipts.append(receipt)
            if receipt['state'] == 'idle':
                break
        else:
            raise RuntimeError('observation_fixture_did_not_converge')
        aliases = {value: key for key, value in ids.items()}
        registry = ObservationRegistry(runtime)
        started = time.perf_counter()
        # No gold topics or expected IDs participate in candidate selection.
        page = registry.related(access, 'query:unspecified-topic', query=step['question'][-256:], limit=16)
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
        elapsed = (time.perf_counter() - started) * 1000
        rows.append({'case_id': step['id'], 'arm': 'observations', 'payload': {
            'retrieval': {'source_ids': [aliases[message_id] for message_id in selected]}, 'evidence': {'citations': citations}},
            'measurements': {'elapsed_ms': elapsed, 'private_bytes': None, 'remaining_children': None}})
        detail.update({'maintenance_receipts': receipts, 'candidate_page': asdict(page), 'status': worker.status(access),
            'publication': 'none; review candidates audited explicitly; no normal-context eligibility asserted'})
        save(root / 'details.json', details)
    save(root / 'observations.json', observations)
    report = score(suite, observations)
    save(root / 'report.json', report)
    save(root / 'scope.json', {'implementation': implementation_identity(),
        'scope': 'Finite candidate-only retrieval/provenance audit using frozen gold. No observation published, no final model response, no ordinary per-turn context, no semantic dedup/model quality or scale admission.',
        'admission': 'pending'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.suite, args.output)
