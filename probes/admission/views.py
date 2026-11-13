"""Frozen-suite audit of actual user-defined bounded views; no chat/model-answer admission."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import time
from typing import Any

from evernook import Change, Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.learning_evaluation import load_suite, score
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.query_plan import plan_query
from evernook.safety import credential_signal
from evernook.topic_views import TopicViewDefinition, TopicViews
from learning import implementation_identity, save


def collect(suite_root: Path, output: Path, embedding_config: Path | None) -> dict[str, Any]:
    suite = load_suite(suite_root)
    root = external(output)
    root.mkdir()
    backend = LocalMiniLM.from_file(embedding_config) if embedding_config else None
    runtimes: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    configuration = {'model': str(asdict(backend.space)) if backend else 'none; lexical fallback',
        'processor': 'uninitialized', 'budget': {'candidate_k': 10, 'max_chars': 8192, 'refresh_timeout': 30,
            'strategy': 'question-defined bounded excerpts; no free generation or host answer'},
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
            recall = SemanticRecall(runtime, backend)
            views = TopicViews(runtime, recall)
            if not runtimes:
                configuration['processor'] = views.processor
            if configuration['processor'] != views.processor:
                raise RuntimeError('view_processor_changed_during_collection')
            runtimes[scenario] = runtime, access, recall, views, {}, {}
        runtime, access, recall, views, ids, events = runtimes[scenario]
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
        while runtime.lexical_status(access).pending_messages:
            runtime.refresh_lexical(access)
        if backend:
            while recall.status(access).pending_messages:
                recall.refresh(access, limit=16, timeout=60)
        aliases = {value: key for key, value in ids.items()}
        started = time.perf_counter()
        payload: dict[str, Any] = {}
        try:
            try:
                current = views.read(access, 'case-topic')
                revision = current.definition_revision
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
            citations = view.data['citations'] if view.data else []
            selected = list(dict.fromkeys(context['message_id'] for context in citations))
            payload = {'retrieval': {'source_ids': [aliases[message_id] for message_id in selected]},
                'evidence': {'citations': [{'source_id': aliases[context['message_id']], **{key: context[key]
                    for key in ('start', 'end', 'quote', 'state_version', 'content_version')}} for context in citations]}}
        except MemoryFailure as error:
            detail['error_code'] = error.code
            if credential_signal(step['question']):
                payload = {'retrieval': {'source_ids': []}, 'evidence': {'citations': []}}
        rows.append({'case_id': step['id'], 'arm': 'views', 'payload': payload,
            'measurements': {'elapsed_ms': (time.perf_counter() - started) * 1000, 'private_bytes': None, 'remaining_children': None}})
        save(root / 'details.json', details)
    save(root / 'observations.json', observations)
    report = score(suite, observations)
    save(root / 'report.json', report)
    save(root / 'scope.json', {'implementation': implementation_identity(), 'scope': 'Actual user-defined view configure/refresh/read with frozen questions and gold-independent queries. No free model inference, automatic publication or final host response. No measured scale/OS or daily admission.',
        'admission': 'pending'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--embedding-config', type=Path)
    args = parser.parse_args()
    collect(args.suite, args.output, args.embedding_config)
