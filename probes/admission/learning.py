"""Collect actual Core observations for zh-learning-v1 into a NEW external directory."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

import evernook
from evernook import Access, Change, Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.automatic_preferences import PreferenceUpdates
from evernook.learning_evaluation import LearningSuite, load_suite, score
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.profile import ProfileRegistry
from evernook.facts import FactRegistry
from evernook.embedding import EmbeddingBackend, ServiceEmbedding
from evernook.archive import capture_control_checkpoint, replay_archive_controls
from evernook.runtime_restore import publish_replayed_runtime
from evernook.purge import PurgeDecision, PurgePreview, PurgeWorkflow


def save(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def implementation_identity(*probe_files: Path) -> dict[str, str]:
    package_root = Path(evernook.__file__).resolve().parent
    digest = hashlib.sha256()
    names = sorted(path.relative_to(package_root).as_posix() for path in package_root.rglob('*')
                   if path.suffix in ('.py', '.mjs') and path.is_file())
    for name in names:
        digest.update(name.encode())
        digest.update((package_root / name).read_bytes())
    digest.update(Path(__file__).read_bytes())
    for path in probe_files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    try:
        package_version = version("evernook")
    except PackageNotFoundError:
        package_version = "uninstalled source"
    return {"sha256": digest.hexdigest(), "package_root": str(package_root), "package_version": package_version}


def previous_processor(python: Path, config: Path, access: Access, current_processor: str, output: Path) -> dict[str, Any]:
    """Use an explicitly supplied installed Core, never fabricate processor hashes."""
    command = '''import hashlib,json,pathlib,sys
from dataclasses import asdict
from importlib.metadata import version
import evernook
from evernook import Access, MemoryRuntime
from evernook.automatic_preferences import PreferenceUpdates
config=pathlib.Path(sys.argv[1])
runtime=MemoryRuntime(config)
access=Access(**json.load(sys.stdin))
updates=PreferenceUpdates(runtime)
settings=updates.settings(access)
if settings.rules_processor==sys.argv[2]: raise SystemExit('unchanged_processor')
updates.configure(access,enabled=True,admitted_processor=settings.rules_processor)
root=pathlib.Path(evernook.__file__).parent
digest=hashlib.sha256()
for path in sorted(root.glob('*.py')):
    digest.update(path.name.encode()); digest.update(path.read_bytes())
tree=hashlib.sha256()
for name in sorted(p.relative_to(root).as_posix() for p in root.rglob('*.py') if p.is_file()):
    tree.update(name.encode()); tree.update((root/name).read_bytes())
print(json.dumps({'settings':asdict(updates.settings(access)),'package_root':str(root),
    'package_version':version('evernook'),'package_sha256':digest.hexdigest(),'package_tree_sha256':tree.hexdigest()}))
'''
    output.mkdir()
    arguments = [str(external(python)), '-I', '-B', '-c', command, str(config), current_processor]
    process = subprocess.run(arguments,
        input=json.dumps(asdict(access)).encode(), capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    (output / 'stdout.bin').write_bytes(process.stdout)
    (output / 'stderr.bin').write_bytes(process.stderr)
    save(output / 'invocation.json', {'command': arguments, 'returncode': process.returncode})
    if process.returncode:
        raise RuntimeError('previous_processor_setup_failed')
    result: dict[str, Any] = json.loads(process.stdout)
    return result


@dataclass
class ScenarioFixture:
    config: Path
    runtime: MemoryRuntime
    access: Access
    ids: dict[str, str] = field(default_factory=dict)
    events: dict[str, Event] = field(default_factory=dict)


class LearningFixtures:
    """Replay the fixed synthetic corpus through public capture and control APIs."""

    def __init__(self, suite: LearningSuite, root: Path, *, full_lifecycle: bool):
        self.suite, self.root, self.full_lifecycle = suite, root, full_lifecycle
        self.scenarios: dict[str, ScenarioFixture] = {}
        self.snapshots: dict[str, Path] = {}
        self.purge_previews: dict[tuple[str, str], PurgePreview] = {}
        self.purge_targets = {(step['scenario'], control['source_id']) for step in suite.steps for control in step['controls']
                              if control['action'] == 'purge'}
        self.restore_scenarios = {step['scenario'] for step in suite.steps if step['phase'] == 'restored'}

    def open(self, scenario: str) -> ScenarioFixture:
        if scenario not in self.scenarios:
            config, access = initialize(self.root / scenario, client_id='synthetic-learning', entry='fixture',
                                        allow_archive_review=self.full_lifecycle)
            self.scenarios[scenario] = ScenarioFixture(config, MemoryRuntime(config), access)
        return self.scenarios[scenario]

    def apply(self, fixture: ScenarioFixture, step: dict[str, Any], detail: dict[str, Any]) -> None:
        scenario = step['scenario']
        if step['phase'] == 'restored':
            self._restore(fixture, scenario, detail)
        runtime, access, ids, events = fixture.runtime, fixture.access, fixture.ids, fixture.events
        receipts = []
        batch_size = 100 if len(step['add']) > 64 else 1
        for begin in range(0, len(step['add']), batch_size):
            aliases = step['add'][begin:begin + batch_size]
            batch = []
            for index, alias in enumerate(aliases):
                source = self.suite.sources[alias]
                batch.append(Event(Source('fixture', source['session'], 'main', alias), source['role'], len(events) + index,
                                   source['at'], 'complete', source['text']))
            saved = runtime.submit_many(access, tuple(batch)) if batch_size > 1 else (runtime.submit(access, batch[0]),)
            for alias, event, receipt in zip(aliases, batch, saved, strict=True):
                ids[alias], events[alias] = receipt.message_id, event
                receipts.append({'source_id': alias, 'receipt': asdict(receipt)})
                if self.full_lifecycle and (scenario, alias) in self.purge_targets:
                    self.purge_previews[scenario, alias] = PurgeWorkflow(runtime).preview(access, 'fixture-' + alias, (receipt.message_id,))
        for alias in step['replay']:
            receipt = runtime.submit(access, events[alias])
            receipts.append({'source_id': alias, 'receipt': asdict(receipt), 'replayed_same_id': receipt.message_id == ids[alias]})
            if receipt.message_id != ids[alias]:
                raise RuntimeError('learning_replay_identity_changed')
        for control in step['controls']:
            target = ids[control['source_id']]
            if control['action'] == 'purge':
                preview = self.purge_previews[scenario, control['source_id']]
                result = asdict(PurgeWorkflow(runtime).purge(access, PurgeDecision(preview.request_id, ids[control['evidence_id']])))
            else:
                state = runtime.state(access, target)
                result = asdict(runtime.change(access, Change('fixture-' + step['id'], target, state.version,
                    control['action'], ids[control['evidence_id']], 'synthetic fixture control')))
            detail.setdefault('controls', []).append(result)
        detail['receipts'] = receipts

    def _restore(self, fixture: ScenarioFixture, scenario: str, detail: dict[str, Any]) -> None:
        runtime, access = fixture.runtime, fixture.access
        checkpoint = capture_control_checkpoint(runtime, access)
        reviewed = self.root / (scenario + '-replayed-archive')
        destination = self.root / (scenario + '-restored-runtime')
        replay_archive_controls(runtime, access, self.snapshots[scenario], reviewed, checkpoint=checkpoint)
        old_rejection = None
        try:
            config, restored_access = publish_replayed_runtime(runtime, access, reviewed, destination,
                checkpoint=checkpoint, client_id='synthetic-learning', entry='fixture')
        except MemoryFailure as failure:
            if failure.code != 'archive_evidence_incomplete':
                raise
            # A control replay cannot synthesize newer derived decisions.
            old_rejection = failure.code
            current = self.root / (scenario + '-current-archive')
            runtime.export_archive(access, current)
            reviewed = self.root / (scenario + '-current-replayed-archive')
            replay_archive_controls(runtime, access, current, reviewed, checkpoint=checkpoint)
            config, restored_access = publish_replayed_runtime(runtime, access, reviewed, destination,
                checkpoint=checkpoint, client_id='synthetic-learning', entry='fixture')
        fixture.config, fixture.runtime, fixture.access = config, MemoryRuntime(config), restored_access
        detail['lifecycle'] = {'operation': 'control_replayed_runtime', 'control_head': checkpoint.control_head,
            'old_archive_publication_rejection': old_rejection, 'published_archive': str(reviewed)}

    def snapshot(self, scenario: str) -> None:
        if self.full_lifecycle and scenario in self.restore_scenarios and scenario not in self.snapshots:
            fixture = self.scenarios[scenario]
            snapshot = self.root / (scenario + '-old-archive')
            fixture.runtime.export_archive(fixture.access, snapshot)
            self.snapshots[scenario] = snapshot


def prepare_indexes(fixture: ScenarioFixture, recall: SemanticRecall, *, lexical_ready: bool,
                    embed: bool, source_count: int) -> None:
    runtime, access = fixture.runtime, fixture.access
    if lexical_ready:
        lexical = runtime.lexical_status(access)
        while lexical.state != 'unavailable' and lexical.pending_messages:
            lexical = runtime.refresh_lexical(access)
        if lexical.state == 'unavailable':
            raise MemoryFailure('lexical_index_unavailable')
    if embed:
        status = recall.status(access)
        while status.state != 'unavailable' and status.pending_messages:
            status = (recall.refresh_window(access, limit=1000, timeout=90) if source_count > 64
                      else recall.refresh(access, limit=16, timeout=60))
        if status.state == 'unavailable':
            raise MemoryFailure('semantic_index_unavailable')


def prepare_case_indexes(fixture: ScenarioFixture, recall: SemanticRecall, detail: dict[str, Any], *,
                         embed: bool, source_count: int) -> bool:
    try:
        prepare_indexes(fixture, recall, lexical_ready=True, embed=embed, source_count=source_count)
    except MemoryFailure as failure:
        if failure.code not in {'storage_busy', 'lexical_index_unavailable', 'semantic_index_unavailable',
                                'semantic_refresh_timeout', 'semantic_refresh_canceled', 'embedding_timeout', 'embedding_canceled'}:
            raise
        detail.update(missing_reason='index_preparation_failed', error_stage='prepare_indexes', error_code=failure.code)
        return False
    return True


def _collect(suite_root: Path, output: Path, backend: EmbeddingBackend | None, *, lexical_ready: bool,
             full_lifecycle: bool, max_fixture_sources: int, previous_core_python: Path | None) -> dict[str, Any]:
    suite = load_suite(suite_root)
    root = external(output)
    root.mkdir()
    fixtures = LearningFixtures(suite, root, full_lifecycle=full_lifecycle)
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    implementation = implementation_identity()
    configuration = {"model": json.dumps(asdict(backend.space), sort_keys=True) if backend else "none; lexical-only baseline",
                     "processor": "pending synthetic runtime initialization",
                     "budget": {"candidate_k": 10, "recall_timeout_seconds": 30,
                                "max_fixture_sources": max_fixture_sources, 'full_lifecycle': full_lifecycle},
                     "policy_sha256": suite.manifest["policy_sha256"],
                     "implementation": implementation["sha256"]}
    if lexical_ready:
        configuration["budget"]["lexical_mode"] = "ready"  # Separate controlled strategy; never relabel old observations.
    observations = {"suite_hash": suite.suite_hash, "configuration": configuration, "rows": rows}
    for step in suite.steps:
        scenario = step["scenario"]
        detail: dict[str, Any] = {"case_id": step["id"], "phase": step["phase"]}
        details.append(detail)
        # These phases require real restore/admission protocols. Never relabel an ordinary query as one.
        if (step['phase'] == 'processor_changed' and previous_core_python is None
                or step['phase'] == 'restored' and not full_lifecycle):
            detail["missing_reason"] = "restore_or_processor_readmission_not_collected"
            save(root / "details.json", details)
            continue
        if (len(step['available_sources']) > max_fixture_sources
                or not full_lifecycle and any(control['action'] == 'purge' for control in step['controls'])):
            detail["missing_reason"] = "scale_or_confirmed_physical_purge_not_collected"
            save(root / "details.json", details)
            continue
        new_scenario = scenario not in fixtures.scenarios
        fixture = fixtures.open(scenario)
        updater = PreferenceUpdates(fixture.runtime)
        if new_scenario:
            settings = updater.settings(fixture.access)
            if len(fixtures.scenarios) == 1:
                configuration["processor"] = settings.rules_processor
            elif configuration["processor"] != settings.rules_processor:
                raise RuntimeError("learning_processor_changed_during_collection")
            updater.configure(fixture.access, enabled=True, admitted_processor=settings.rules_processor)
            detail["processor"] = settings.rules_processor
        if step['phase'] == 'processor_changed':
            assert previous_core_python is not None
            before = updater.settings(fixture.access)
            legacy = previous_processor(previous_core_python, fixture.config, fixture.access, before.rules_processor,
                                        root / (step['id'] + '-previous-core'))
            fixture.runtime = MemoryRuntime(fixture.config)
            updater = PreferenceUpdates(fixture.runtime)
            after = updater.settings(fixture.access)
            if after.admitted_processor == after.rules_processor or after.admitted_processor != legacy['settings']['rules_processor']:
                raise RuntimeError('processor_transition_not_observed')
            detail['lifecycle'] = {'operation': 'real_core_version_transition', 'previous': legacy,
                'current': asdict(after), 'readmitted': False,
                'scope': 'Explicit previous installed Core configured its real rules on this synthetic runtime; current Core reopened without re-admission.'}
        fixtures.apply(fixture, step, detail)
        runtime, access, ids, events = fixture.runtime, fixture.access, fixture.ids, fixture.events
        if step['phase'] == 'restored':
            updater = PreferenceUpdates(runtime)
            detail['lifecycle']['settings_after_restore'] = asdict(updater.settings(access))
        reports = []
        for alias in step["add"] + step["replay"]:
            if len(step['available_sources']) > 64 and not updater.has_candidates(events[alias].text):
                detail['noncandidate_sources_skipped'] = detail.get('noncandidate_sources_skipped', 0) + 1
                continue
            reports.append(updater.process(access, (ids[alias],)))
        aliases = {value: key for key, value in ids.items()}
        payload: dict[str, Any] = {}
        recall = SemanticRecall(runtime, backend)
        prepare_indexes(fixture, recall, lexical_ready=lexical_ready, embed=backend is not None,
                        source_count=len(step['available_sources']))
        started = time.perf_counter()
        try:
            page = recall.recall(access, step["question"], limit=10, as_of=step["as_of"], timeout=30)
            detail["recall"] = asdict(page)
            payload["retrieval"] = {"source_ids": [aliases[hit.evidence.message_id] for hit in page.hits]}
            payload["evidence"] = {"citations": [{"source_id": aliases[hit.evidence.message_id],
                "start": span.start, "end": span.end, "quote": span.text,
                "state_version": hit.evidence.state_version, "content_version": hit.evidence.content_version}
                for hit in page.hits for span in hit.evidence.spans]}
        except MemoryFailure as failure:
            detail["recall_error_code"] = failure.code
        elapsed = (time.perf_counter() - started) * 1000
        summary = ProfileRegistry(runtime).summary(access)
        pending = [item for report in reports for item in report.updates if item.state == 'pending_clarification']
        if pending:
            clarification_sources: set[str] = set()
            for item in pending:
                if item.predicate:
                    view = FactRegistry(runtime).query(access, 'user', item.predicate)
                    for record in (*view.disputes, *((view.fact,) if view.fact else ())):
                        clarification_sources.update(span.message_id for span in record.sources)
                if item.profile_tag_id:
                    profile = ProfileRegistry(runtime).read(access, item.profile_tag_id)
                    if profile.record:
                        clarification_sources.update(span.message_id for span in profile.record.draft.sources)
            if clarification_sources:
                payload['automatic'] = {'decision': 'clarify', 'value': None,
                    'source_ids': sorted(aliases[source_id] for source_id in clarification_sources)}
            else:
                detail['automatic_missing_reason'] = 'clarification_without_current_sources'
        elif len(summary.tags) == 1:
            tag = summary.tags[0]
            payload["automatic"] = {"decision": "accept", "value": tag.draft.label,
                "source_ids": sorted({aliases[span.message_id] for span in tag.draft.sources})}
        elif not summary.tags:
            payload["automatic"] = {"decision": "review", "value": None, "source_ids": []}
        else:
            detail["automatic_missing_reason"] = "multiple_tags_need_fieldwise_projection"
        # A pre-canceled public recall must return its error before starting work.
        cancel = threading.Event()
        cancel.set()
        cancellation = None
        try:
            recall.recall(access, "synthetic cancellation check", cancel=cancel)
        except MemoryFailure as failure:
            cancellation = failure.code
        detail.update({"automatic_reports": [asdict(report) for report in reports],
            "profile": asdict(summary), "status": asdict(runtime.status(access)), "lexical": asdict(runtime.lexical_status(access)),
            "semantic": asdict(recall.status(access)), "cancellation": {"kind": "pre_canceled", "error_code": cancellation,
                "in_flight_cancellation": "missing", "residual_processes": None}})
        rows.append({"case_id": step["id"], "arm": "raw", "payload": payload,
                     "measurements": {"elapsed_ms": elapsed, "private_bytes": None, "remaining_children": None}})
        save(root / "observations.json", observations)
        save(root / "details.json", details)
        fixtures.snapshot(scenario)
        print(json.dumps({"case_id": step["id"], "collected": sorted(payload)}), flush=True)
    save(root / "report.json", score(suite, observations))
    save(root / "collection.json", {"suite_hash": suite.suite_hash, "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "implementation": implementation,
        "scope": "Actual public Core queries and synthetically enabled finite preference rules; explicit full-lifecycle mode performs archive restore and confirmed synthetic purge. No chat model, host answer, derived observation/view/reflection or model admission run.",
        "latency_scope": "Recall call only; fixture initialization and automatic processing excluded. Heterogeneous small-fixture distribution, not scale or #81 latency admission.",
        "resource_scope": "Per-row resource fields unmeasured; run under existing monitor.py for external process-tree samples.",
        "missing_phases": ([] if previous_core_python else ['processor_changed']) + ([] if full_lifecycle else ['restored'])})
    return observations


def collect(suite_root: Path, output: Path, embedding_config: Path | None = None, *, lexical_ready: bool = False,
            full_lifecycle: bool = False, max_fixture_sources: int = 64, reuse_embedding: bool = False,
            previous_core_python: Path | None = None) -> dict[str, Any]:
    if type(max_fixture_sources) is not int or not 1 <= max_fixture_sources <= 20000:
        raise ValueError('invalid_fixture_source_limit')
    binding = ServiceEmbedding(embedding_config) if reuse_embedding and embedding_config is not None else None
    try:
        backend = (binding.current() if binding is not None else
                   LocalMiniLM.from_file(embedding_config) if embedding_config is not None else None)
        return _collect(suite_root, output, backend, lexical_ready=lexical_ready,
                        full_lifecycle=full_lifecycle, max_fixture_sources=max_fixture_sources, previous_core_python=previous_core_python)
    finally:
        if binding is not None:
            binding.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-learning-v1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--lexical-ready", action="store_true")
    parser.add_argument('--full-lifecycle', action='store_true')
    parser.add_argument('--max-fixture-sources', type=int, default=64)
    parser.add_argument('--reuse-embedding', action='store_true')
    parser.add_argument('--previous-core-python', type=Path)
    args = parser.parse_args()
    collect(args.suite, args.output, args.embedding_config, lexical_ready=args.lexical_ready,
            full_lifecycle=args.full_lifecycle, max_fixture_sources=args.max_fixture_sources, reuse_embedding=args.reuse_embedding,
            previous_core_python=args.previous_core_python)
    return 0  # Successful collection does not assert successful quality or admission.


if __name__ == "__main__":
    raise SystemExit(main())
