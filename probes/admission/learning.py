"""Collect actual Core observations for zh-learning-v1 into a NEW external directory."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import threading
import time
from typing import Any

import evernook
from evernook import Change, Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.automatic_preferences import PreferenceUpdates
from evernook.learning_evaluation import load_suite, score
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.profile import ProfileRegistry
from evernook.facts import FactRegistry


def save(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def implementation_identity() -> dict[str, str]:
    package_root = Path(evernook.__file__).resolve().parent
    digest = hashlib.sha256()
    for module in sorted(package_root.glob("*.py")):
        digest.update(module.name.encode())
        digest.update(module.read_bytes())
    digest.update(Path(__file__).read_bytes())
    try:
        package_version = version("evernook")
    except PackageNotFoundError:
        package_version = "uninstalled source"
    return {"sha256": digest.hexdigest(), "package_root": str(package_root), "package_version": package_version}


def collect(suite_root: Path, output: Path, embedding_config: Path | None = None, *, lexical_ready: bool = False) -> dict[str, Any]:
    suite = load_suite(suite_root)
    backend = LocalMiniLM.from_file(embedding_config) if embedding_config is not None else None
    root = external(output)
    root.mkdir()
    runtimes: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    implementation = implementation_identity()
    configuration = {"model": json.dumps(asdict(backend.space), sort_keys=True) if backend else "none; lexical-only baseline",
                     "processor": "pending synthetic runtime initialization",
                     "budget": {"candidate_k": 10, "recall_timeout_seconds": 30, "max_fixture_sources": 64},
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
        if step["phase"] in {"restored", "processor_changed"}:
            detail["missing_reason"] = "restore_or_processor_readmission_not_collected"
            save(root / "details.json", details)
            continue
        if len(step["available_sources"]) > 64 or any(control["action"] == "purge" for control in step["controls"]):
            detail["missing_reason"] = "scale_or_confirmed_physical_purge_not_collected"
            save(root / "details.json", details)
            continue
        if scenario not in runtimes:
            config, access = initialize(root / scenario, client_id="synthetic-learning", entry="fixture")
            runtime = MemoryRuntime(config)
            updater = PreferenceUpdates(runtime)
            settings = updater.settings(access)
            if not runtimes:
                configuration["processor"] = settings.rules_processor
            elif configuration["processor"] != settings.rules_processor:
                raise RuntimeError("learning_processor_changed_during_collection")
            updater.configure(access, enabled=True, admitted_processor=settings.rules_processor)
            runtimes[scenario] = (runtime, access, updater, {}, {})
            detail["processor"] = settings.rules_processor
        runtime, access, updater, ids, events = runtimes[scenario]
        receipts = []
        reports = []
        for alias in step["add"]:
            source = suite.sources[alias]
            event = Event(Source("fixture", source["session"], "main", alias), source["role"], len(events),
                          source["at"], "complete", source["text"])
            receipt = runtime.submit(access, event)
            ids[alias], events[alias] = receipt.message_id, event
            receipts.append({"source_id": alias, "receipt": asdict(receipt)})
        for alias in step["replay"]:
            receipt = runtime.submit(access, events[alias])
            receipts.append({"source_id": alias, "receipt": asdict(receipt), "replayed_same_id": receipt.message_id == ids[alias]})
            if receipt.message_id != ids[alias]:
                raise RuntimeError("learning_replay_identity_changed")
        for control in step["controls"]:
            target = ids[control["source_id"]]
            state = runtime.state(access, target)
            runtime.change(access, Change("fixture-" + step["id"], target, state.version,
                control["action"], ids[control["evidence_id"]], "synthetic fixture control"))
        for alias in step["add"] + step["replay"]:
            reports.append(updater.process(access, (ids[alias],)))
        aliases = {value: key for key, value in ids.items()}
        payload: dict[str, Any] = {}
        recall = SemanticRecall(runtime, backend)
        if lexical_ready:
            while runtime.lexical_status(access).pending_messages:
                runtime.refresh_lexical(access)
        if backend is not None:
            status = recall.status(access)
            while status.pending_messages:
                status = recall.refresh(access, limit=16, timeout=60)
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
        detail.update({"receipts": receipts, "automatic_reports": [asdict(report) for report in reports],
            "profile": asdict(summary), "status": asdict(runtime.status(access)), "lexical": asdict(runtime.lexical_status(access)),
            "semantic": asdict(recall.status(access)), "cancellation": {"kind": "pre_canceled", "error_code": cancellation,
                "in_flight_cancellation": "missing", "residual_processes": None}})
        rows.append({"case_id": step["id"], "arm": "raw", "payload": payload,
                     "measurements": {"elapsed_ms": elapsed, "private_bytes": None, "remaining_children": None}})
        save(root / "observations.json", observations)
        save(root / "details.json", details)
        print(json.dumps({"case_id": step["id"], "collected": sorted(payload)}), flush=True)
    save(root / "report.json", score(suite, observations))
    save(root / "collection.json", {"suite_hash": suite.suite_hash, "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "implementation": implementation,
        "scope": "Actual public Core queries (pinned local embedding when explicitly configured) and synthetically enabled finite preference rules. No chat model, host answer, derived observation/view/reflection, restore or model admission run.",
        "latency_scope": "Recall call only; fixture initialization and automatic processing excluded. Heterogeneous small-fixture distribution, not scale or #81 latency admission.",
        "resource_scope": "Per-row resource fields unmeasured; run under existing monitor.py for external process-tree samples.",
        "missing_phases": ["processor_changed", "restored"]})
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-learning-v1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--lexical-ready", action="store_true")
    args = parser.parse_args()
    collect(args.suite, args.output, args.embedding_config, lexical_ready=args.lexical_ready)
    return 0  # Successful collection does not assert successful quality or admission.


if __name__ == "__main__":
    raise SystemExit(main())
