"""Frozen-suite audit of actual user-defined bounded views; no chat/model-answer admission."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
import time
from typing import Any

from evernook import LocalMiniLM, SemanticRecall
from evernook.learning_evaluation import load_suite, score
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.query_plan import plan_query
from evernook.topic_views import TopicViewDefinition, TopicViews

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from probes.admission.learning import LearningFixtures, implementation_identity, prepare_case_indexes, save


def collect(suite_root: Path, output: Path, embedding_config: Path | None, *, full_lifecycle: bool = False,
            max_fixture_sources: int = 64) -> dict[str, Any]:
    if type(max_fixture_sources) is not int or not 1 <= max_fixture_sources <= 20000:
        raise ValueError('invalid_fixture_source_limit')
    suite = load_suite(suite_root)
    root = external(output)
    root.mkdir()
    backend = LocalMiniLM.from_file(embedding_config) if embedding_config else None
    fixtures = LearningFixtures(suite, root, full_lifecycle=full_lifecycle)
    managers: dict[str, TopicViews] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    implementation = implementation_identity(Path(__file__))
    configuration = {'model': str(asdict(backend.space)) if backend else 'none; lexical fallback',
        'processor': 'uninitialized', 'budget': {'candidate_k': 10, 'max_chars': 8192, 'refresh_timeout': 30,
            'max_fixture_sources': max_fixture_sources, 'full_lifecycle': full_lifecycle,
            'time_control': 'current-only snapshots; no as_of control', 'strategy': 'question-defined bounded excerpts'},
        'policy_sha256': suite.manifest['policy_sha256'], 'implementation': implementation['sha256']}
    observations = {'suite_hash': suite.suite_hash, 'configuration': configuration, 'rows': rows}
    for step in suite.steps:
        phase = step['phase']
        source_count = len(step['available_sources'])
        detail: dict[str, Any] = {'case_id': step['id'], 'phase': phase, 'corpus_sources': source_count, 'frozen_as_of': step['as_of']}
        details.append(detail)
        if phase == 'processor_changed':
            detail['missing_reason'] = 'view_processor_transition_not_collected'
        elif source_count > max_fixture_sources:
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
        views = managers.get(scenario)
        if views is None or views.runtime is not runtime:
            recall = SemanticRecall(runtime, backend)
            views = TopicViews(runtime, recall)
            if not managers:
                configuration['processor'] = views.processor
            if configuration['processor'] != views.processor:
                raise RuntimeError('view_processor_changed_during_collection')
            managers[scenario] = views
        if not prepare_case_indexes(fixture, views.recall, detail, embed=backend is not None, source_count=source_count):
            save(root / 'details.json', details)
            fixtures.snapshot(scenario)
            continue
        aliases = {value: key for key, value in fixture.ids.items()}
        started = time.perf_counter()
        payload: dict[str, Any] = {}
        try:
            try:
                current = views.read(access, 'case-topic')
                revision = current.definition_revision
                detail['view_before_definition'] = asdict(current)
            except MemoryFailure as error:
                if error.code != 'topic_view_not_found':
                    raise
                revision = 0
            query = plan_query(step['question']).query
            definition = TopicViewDefinition('冻结问题专题', '为该冻结问题构建有界原始依据背景', query, max_sources=10)
            views.configure(access, 'define-' + step['id'], 'case-topic', revision, definition)
            views.refresh(access, 'case-topic', timeout=30)
            refresh_ms = (time.perf_counter() - started) * 1000
            reading = time.perf_counter()
            view = views.read(access, 'case-topic')
            detail.update({'view': asdict(view), 'refresh_and_definition_ms': refresh_ms,
                'read_ms': (time.perf_counter() - reading) * 1000})
            if view.data is not None or view.state == 'empty':
                citations = view.data['citations'] if view.data is not None else []
                selected = list(dict.fromkeys(context['message_id'] for context in citations))
                payload = {'retrieval': {'source_ids': [aliases[message_id] for message_id in selected]},
                    'evidence': {'citations': [{'source_id': aliases[context['message_id']], **{key: context[key]
                        for key in ('start', 'end', 'quote', 'state_version', 'content_version')}} for context in citations]}}
            else:
                detail['missing_reason'] = 'snapshot_unavailable'
        except MemoryFailure as error:
            detail['error_code'] = error.code
        rows.append({'case_id': step['id'], 'arm': 'views', 'payload': payload,
            'measurements': {'elapsed_ms': (time.perf_counter() - started) * 1000, 'private_bytes': None, 'remaining_children': None}})
        fixtures.snapshot(scenario)
        save(root / 'details.json', details)
        save(root / 'observations.json', observations)
    save(root / 'observations.json', observations)
    report = score(suite, observations)
    save(root / 'report.json', report)
    save(root / 'scope.json', {'implementation': implementation,
        'scope': 'Actual bounded-topic snapshots from gold-independent questions and original excerpts. Unavailable snapshots remain missing. Observation publication, host outputs and model judgments remain unmeasured.',
        'admission': 'pending'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--embedding-config', type=Path)
    parser.add_argument('--full-lifecycle', action='store_true')
    parser.add_argument('--max-fixture-sources', type=int, default=64)
    args = parser.parse_args()
    collect(args.suite, args.output, args.embedding_config, full_lifecycle=args.full_lifecycle,
            max_fixture_sources=args.max_fixture_sources)
