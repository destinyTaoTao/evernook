"""Bounded frozen-case local-model reflection pilot; missing cases remain missing."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import patch

from evernook import Event, MemoryRuntime, Source, initialize
from evernook.embedding import LocalMiniLM
from evernook.learning_evaluation import load_suite, score
from evernook.reflection_model import from_file
from evernook.semantic import SemanticRecall
from evernook.paths import external
from learning import implementation_identity, save
from reflection_projection import project_result


def collect(suite_root: Path, output: Path, model_config: Path, embedding_config: Path | None, cases: tuple[str, ...]) -> dict[str, Any]:
    suite = load_suite(suite_root)
    if len(set(cases)) != len(cases) or not 1 <= len(cases) <= 8 or any(case not in suite.cases or suite.cases[case]['phase'] != 'initial' for case in cases):
        raise ValueError('pilot_requires_one_to_eight_initial_cases')
    root = external(output); root.mkdir()
    backend = LocalMiniLM.from_file(embedding_config) if embedding_config else None
    rows: list[dict[str, Any]] = []
    configuration: dict[str, Any] = {'model': 'uninitialized', 'processor': 'uninitialized',
        'budget': {'candidate_k': 10, 'cases': list(cases), 'strategy': 'explicit temporary local-model reflect; no host answer or automatic publication'},
        'policy_sha256': suite.manifest['policy_sha256'], 'implementation': implementation_identity()['sha256']}
    observations = {'suite_hash': suite.suite_hash, 'configuration': configuration, 'rows': rows}
    details = []
    for step in suite.steps:
        if step['id'] not in cases:
            continue
        config, access = initialize(root / step['id'], client_id='synthetic', entry='fixture')
        runtime = MemoryRuntime(config)
        recall = SemanticRecall(runtime, backend)
        engine = from_file(runtime, recall, model_config)
        engine.configure(access)
        if configuration['model'] == 'uninitialized':
            configuration['model'] = engine.model.processor_version if engine.model else 'none'
            configuration['processor'] = engine.identity
            configuration['budget']['effective_policy'] = asdict(engine.policy)
        if configuration['processor'] != engine.identity:
            raise RuntimeError('processor_changed_during_collection')
        aliases = {}
        for index, alias in enumerate(step['available_sources']):
            source = suite.sources[alias]
            receipt = runtime.submit(access, Event(Source('fixture', source['session'], 'main', alias), source['role'], index,
                source['at'], 'complete', source['text']))
            aliases[receipt.message_id] = alias
        while runtime.lexical_status(access).pending_messages:
            runtime.refresh_lexical(access)
        if backend:
            while recall.status(access).pending_messages:
                recall.refresh(access, limit=16, timeout=60)
        wire: dict[int, dict[str, Any]] = {}
        class TraceProcess(subprocess.Popen[bytes]):
            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                output, error = super().communicate(input=input, timeout=timeout)
                if output and 'reflection_worker.mjs' in str(self.args):
                    # Frozen synthetic fixtures only. Deduplicate final communicate() by PID.
                    wire[self.pid] = json.loads(output)
                return output, error
        with patch('evernook.reflection_model.subprocess.Popen', TraceProcess):
            result = engine.reflect(access, step['question'])
        payload = project_result(result, aliases)
        rows.append({'case_id': step['id'], 'arm': 'reflect', 'payload': payload,
            'measurements': {'elapsed_ms': result['usage'].get('elapsed_ms', 0), 'private_bytes': None, 'remaining_children': None}})
        details.append({'case_id': step['id'], 'result': result, 'model_wire': list(wire.values()),
            'projection': 'actual assessment carries answer/abstain/refuse/clarify and reason citations; fallback is separate; finite guard origin is not model success'})
        save(root / 'details.json', details)
        save(root / 'observations.json', observations)
        print({'case_id': step['id'], 'state': result['state'], 'degraded': result['degraded']}, flush=True)
    report = score(suite, observations)
    save(root / 'report.json', report)
    save(root / 'scope.json', {'implementation': implementation_identity(), 'scope': 'Selected initial frozen cases only; fixed local Qwen plus optional MiniLM. Failure fallback abstention is mechanically projected, not model reasoning success. No host answer, maintenance or automatic updates. Missing other cases retained.',
        'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'completed_model_cases': [item['case_id'] for item in details if item['result']['state'] == 'complete'], 'admission': 'pending'})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--embedding-config', type=Path)
    parser.add_argument('--cases', default='old-initial,unknown-initial,multi-initial,insufficient-initial,conflict-initial,injection-initial')
    args = parser.parse_args()
    collect(args.suite, args.output, args.model_config, args.embedding_config, tuple(args.cases.split(',')))
