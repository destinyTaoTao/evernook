"""Collect explicit local-model reflection on the frozen synthetic learning suite."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

from evernook.embedding import EmbeddingBackend, LocalMiniLM, ServiceEmbedding
from evernook.learning_evaluation import LearningSuite, load_suite, score
from evernook.paths import external
from evernook.reflection import ReflectionEngine
from evernook.reflection_model import LocalReflectionModel, from_file
from evernook.semantic import SemanticRecall

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from probes.admission.learning import LearningFixtures, ScenarioFixture, implementation_identity, prepare_case_indexes, save
from probes.admission.reflection_projection import project_result


DEFAULT_CASES = ('old-initial', 'unknown-initial', 'multi-initial', 'insufficient-initial', 'conflict-initial', 'injection-initial')


def _profile_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected(suite: LearningSuite, cases: tuple[str, ...] | None, full_lifecycle: bool) -> set[str]:
    if cases is None:
        cases = tuple(suite.cases) if full_lifecycle else DEFAULT_CASES
    selected = set(cases)
    if not selected or len(cases) != len(selected) or not selected.issubset(suite.cases):
        raise ValueError('invalid_reflection_cases')
    if not full_lifecycle and (len(selected) > 8 or any(suite.cases[key]['phase'] != 'initial' for key in selected)):
        raise ValueError('pilot_requires_one_to_eight_initial_cases')
    return selected


def _invoke(engine: ReflectionEngine, fixture: ScenarioFixture, question: str, output: Path) -> dict[str, Any]:
    output.mkdir()
    attempts: list[dict[str, Any]] = []
    wire: list[dict[str, Any]] = []

    class TraceProcess(subprocess.Popen[bytes]):
        trace_dir: Path | None = None
        recorded = False

        def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
            worker = isinstance(self.args, list) and len(self.args) == 2 and str(self.args[1]).endswith('reflection_worker.mjs')
            if worker and self.trace_dir is None:
                self.trace_dir = output / f'turn-{len(attempts) + 1}'
                self.trace_dir.mkdir()
                record = {'pid': self.pid, 'transcript': self.trace_dir.name, 'returncode': None}
                attempts.append(record)
                if input is not None:
                    (self.trace_dir / 'stdin.bin').write_bytes(input)
            stdout, stderr = super().communicate(input=input, timeout=timeout)
            if self.trace_dir is not None and not self.recorded:
                self.recorded = True
                (self.trace_dir / 'stdout.bin').write_bytes(stdout)
                (self.trace_dir / 'stderr.bin').write_bytes(stderr)
                attempts[-1]['returncode'] = self.returncode
                try:
                    parsed = json.loads(stdout)
                except (ValueError, RecursionError):
                    pass  # Preserve failed output as bytes without masking the protocol result.
                else:
                    if isinstance(parsed, dict):
                        wire.append(parsed)
            return stdout, stderr

    started = time.perf_counter()
    with patch('evernook.reflection_model.subprocess.Popen', TraceProcess):
        result = engine.reflect(fixture.access, question)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {'result': result, 'model_wire': wire, 'model_attempts': attempts, 'elapsed_ms': elapsed_ms}


def _collect(suite: LearningSuite, root: Path, model_config: Path, backend: EmbeddingBackend | None, selected: set[str], *,
             full_lifecycle: bool, max_fixture_sources: int, reuse_embedding: bool) -> dict[str, Any]:
    fixtures = LearningFixtures(suite, root, full_lifecycle=full_lifecycle)
    engines: dict[str, ReflectionEngine] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    judgment_origins: Counter[str] = Counter()
    implementation = implementation_identity(Path(__file__), Path(__file__).with_name('reflection_projection.py'))
    configuration: dict[str, Any] = {'model': 'uninitialized', 'processor': 'uninitialized',
        'budget': {'candidate_k': 10, 'cases': [step['id'] for step in suite.steps if step['id'] in selected],
                   'mode': 'deep', 'max_fixture_sources': max_fixture_sources, 'full_lifecycle': full_lifecycle,
                   'embedding': asdict(backend.space) if backend is not None else None, 'reuse_embedding': reuse_embedding,
                   'time_control': 'current-only; Core reflect does not accept as_of'},
        'policy_sha256': suite.manifest['policy_sha256'], 'implementation': implementation['sha256']}
    observations = {'suite_hash': suite.suite_hash, 'configuration': configuration, 'rows': rows}
    profile_digest = _profile_digest(model_config)
    scope = {'implementation': implementation, 'model_config_sha256': profile_digest,
        'scope': 'Public Core deep reflection with fixed local Qwen and optional MiniLM. Lifecycle fixtures use actual original capture, replay, source controls, confirmed purge and reviewed publication. Raw worker envelopes and guarded assessments are preserved. Model or timeout fallback is unscored; explicit Core protected routing is recorded as refusal.',
        'time_control': 'Original frozen question and source timestamps; current-only Core reflect API has no as_of argument.',
        'latency_scope': 'Public reflect call with transcript capture; excludes fixture, configure and index preparation.',
        'admission': 'pending'}
    save(root / 'scope.json', scope)
    save(root / 'observations.json', observations)
    last = {step['scenario']: step['id'] for step in suite.steps if step['id'] in selected}
    for step in suite.steps:
        scenario, key, phase = step['scenario'], step['id'], step['phase']
        if scenario not in last:
            continue
        if key == last[scenario]:
            del last[scenario]
        detail: dict[str, Any] = {'case_id': key, 'phase': phase, 'selected': key in selected, 'frozen_as_of': step['as_of']}
        details.append(detail)
        if phase == 'processor_changed':
            detail['missing_reason'] = 'reflection_processor_transition_not_collected'
        elif len(step['available_sources']) > max_fixture_sources:
            detail['missing_reason'] = 'fixture_source_limit'
        else:
            fixture = fixtures.open(scenario)
            fixtures.apply(fixture, step, detail)
            if key in selected:
                engine = engines.get(scenario)
                if engine is None or engine.runtime is not fixture.runtime:
                    recall = SemanticRecall(fixture.runtime, backend)
                    if _profile_digest(model_config) != profile_digest:
                        raise RuntimeError('reflection_model_config_changed_during_collection')
                    engine = from_file(fixture.runtime, recall, model_config)
                    if _profile_digest(model_config) != profile_digest:
                        raise RuntimeError('reflection_model_config_changed_during_collection')
                    if not isinstance(engine.model, LocalReflectionModel):
                        raise ValueError('local_reflection_profile_required')
                    detail['reflection_configure'] = engine.configure(fixture.access)
                    engines[scenario] = engine
                    if configuration['model'] == 'uninitialized':
                        configuration['model'] = engine.model.processor_version
                        configuration['processor'] = engine.identity
                        configuration['budget']['effective_policy'] = asdict(engine.policy)
                    elif configuration['processor'] != engine.identity:
                        raise RuntimeError('reflection_processor_changed_during_collection')
                if prepare_case_indexes(fixture, engine.recall, detail, embed=backend is not None,
                                        source_count=len(step['available_sources'])):
                    detail.update(_invoke(engine, fixture, step['question'], root / (key + '-workers')))
                    result = detail['result']
                    aliases = {message_id: alias for alias, message_id in fixture.ids.items()}
                    payload = project_result(result, aliases)
                    if 'judgment' in payload:
                        assessment = result['assessment']
                        origin = assessment['origin'] if assessment is not None else 'protected_route'
                        detail['judgment_origin'] = origin
                        judgment_origins[origin] += 1
                    else:
                        detail['judgment_missing_reason'] = result['stop_reason']
                    rows.append({'case_id': key, 'arm': 'reflect', 'payload': payload,
                        'measurements': {'elapsed_ms': detail['elapsed_ms'], 'private_bytes': None, 'remaining_children': None}})
                    print({'case_id': key, 'state': result['state'], 'stop_reason': result['stop_reason']}, flush=True)
            fixtures.snapshot(scenario)
        save(root / 'details.json', details)
        save(root / 'observations.json', observations)
    report = score(suite, observations)
    save(root / 'report.json', report)
    scope.update(completed_model_cases=[detail['case_id'] for detail in details if detail.get('result', {}).get('state') == 'complete'],
                 protocol_states=dict(Counter(detail['result']['state'] for detail in details if 'result' in detail)),
                 judgment_origins=dict(judgment_origins))
    save(root / 'scope.json', scope)
    return report


def collect(suite_root: Path, output: Path, model_config: Path, embedding_config: Path | None,
            cases: tuple[str, ...] | None, *, full_lifecycle: bool = False, max_fixture_sources: int = 64,
            reuse_embedding: bool = False) -> dict[str, Any]:
    if type(max_fixture_sources) is not int or not 1 <= max_fixture_sources <= 20000:
        raise ValueError('invalid_fixture_source_limit')
    suite = load_suite(suite_root)
    selected = _selected(suite, cases, full_lifecycle)
    root, model_config = external(output), external(model_config)
    root.mkdir()
    binding = ServiceEmbedding(embedding_config) if reuse_embedding and embedding_config is not None else None
    try:
        backend = (binding.current() if binding is not None else
                   LocalMiniLM.from_file(embedding_config) if embedding_config is not None else None)
        return _collect(suite, root, model_config, backend, selected, full_lifecycle=full_lifecycle,
                        max_fixture_sources=max_fixture_sources, reuse_embedding=reuse_embedding)
    finally:
        if binding is not None:
            binding.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=Path('eval/zh-learning-v1'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--embedding-config', type=Path)
    parser.add_argument('--cases')
    parser.add_argument('--full-lifecycle', action='store_true')
    parser.add_argument('--max-fixture-sources', type=int, default=64)
    parser.add_argument('--reuse-embedding', action='store_true')
    args = parser.parse_args()
    collect(args.suite, args.output, args.model_config, args.embedding_config,
            None if args.cases is None else tuple(args.cases.split(',')), full_lifecycle=args.full_lifecycle,
            max_fixture_sources=args.max_fixture_sources, reuse_embedding=args.reuse_embedding)
