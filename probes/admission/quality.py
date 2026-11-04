"""Public API observations and isolated host fixtures for the frozen Chinese suite."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from evernook import CandidateDraft, CandidateWorkflow, Event, EvidenceSpan, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize, pair_client
from evernook.automatic_preferences import PreferenceUpdates
from evernook.evaluation import EvaluationSuite, load_suite, run_core, score
from evernook.facts import FactDecision, FactRecord, FactRegistry
from evernook.paths import external
from evernook.profile import ProfileRegistry


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def seed(suite: EvaluationSuite, root: Path, source_ids: list[str], *, facts_enabled: bool) -> dict[str, Any]:
    config, owner = initialize(root, client_id="synthetic-fixture-owner", entry="fixture")
    runtime = MemoryRuntime(config)
    originals = {row["id"]: row for row in suite.corpus["sources"] if row["id"] in source_ids}
    ids = {}
    for index, (alias, row) in enumerate(originals.items()):
        receipt = runtime.submit(owner, Event(Source("fixture", row.get("session", "fixture"), "main", alias),
                                row["role"], index, row["at"], "complete", row["text"]))
        ids[alias] = receipt.message_id
    records: dict[str, FactRecord] = {}
    if facts_enabled:
        workflow = CandidateWorkflow(runtime)
        registry = FactRegistry(runtime)
        revisions: dict[tuple[str, str], int] = {}
        slots = {(row["subject"], row["predicate"]) for row in suite.corpus["facts"] if row["source_id"] in ids}
        complete_slots = {slot for slot in slots if all(row["source_id"] in ids for row in suite.corpus["facts"]
            if (row["subject"], row["predicate"]) == slot)}
        for fact in suite.corpus["facts"]:
            alias = fact["source_id"]
            if alias not in ids or (fact["subject"], fact["predicate"]) not in complete_slots:
                continue
            text = originals[alias]["text"]
            lease = workflow.begin(owner, (ids[alias],), processor_version="synthetic-evaluation-v1")
            if lease.token is None:
                raise ValueError("fixture_candidate_lease_missing")
            workflow.complete(owner, lease.status.run_id, lease.token,
                (CandidateDraft(text, "user_statement", (EvidenceSpan(ids[alias], 1, "original", 1, 0, len(text), text),)),))
            candidate = workflow.candidates(owner, lease.status.run_id)[0]
            slot = (fact["subject"], fact["predicate"])
            records[fact["id"]] = registry.decide(owner, FactDecision(fact["id"], *slot, candidate.candidate_id,
                ids[alias], revisions.get(slot, 0), fact["kind"], fact["valid_from"],
                replaces_id=records[fact["replaces"]].fact_id if fact["replaces"] else None,
                resolves=tuple(records[item].fact_id for item in fact["resolves"])))
            revisions[slot] = revisions.get(slot, 0) + 1
    return {"config": config, "owner": owner, "runtime": runtime, "ids": ids, "facts": records}


def automatic(suite: EvaluationSuite, root: Path) -> dict[str, Any]:
    queries = {query["id"]: query for query in suite.corpus["queries"]}
    rows = []
    details = []
    for case in suite.cases:
        if case.dimension != "auto_acceptance":
            continue
        query = queries[case.query_id]
        prepared = seed(suite, root / str(query["id"]), query["context_sources"], facts_enabled=False)
        runtime, owner, ids = prepared["runtime"], prepared["owner"], prepared["ids"]
        updater = PreferenceUpdates(runtime)
        settings = updater.settings(owner)
        updater.configure(owner, enabled=True, admitted_processor=settings.rules_processor)
        reports = [updater.process(owner, (message_id,)) for message_id in ids.values()]
        summary = ProfileRegistry(runtime).summary(owner)
        aliases = {value: key for key, value in ids.items()}
        pending = any(item.clarification_id for report in reports for item in report.updates)
        payload: dict[str, Any] = {"decision": "clarify" if pending else "review", "value": None, "source_ids": []}
        if summary.tags and not pending:
            tag = summary.tags[0]
            payload = {"decision": "accept", "value": tag.draft.label,
                       "source_ids": sorted({aliases.get(span.message_id, "unmapped-source") for span in tag.draft.sources})}
        rows.append({"id": case.id, "payload": payload})
        details.append({"query_id": query["id"], "rules_processor": settings.rules_processor,
                        "reports": [asdict(report) for report in reports], "profile": asdict(summary),
                        "collection": "No accepted profile or pending clarification is reported conservatively as review, not fabricated reject."})
        save(root / "observations.json", {"suite_hash": suite.suite_hash, "rows": rows})
        save(root / "automatic-details.json", details)
        print(json.dumps({"phase": "automatic", "query_id": query["id"], "decision": payload["decision"]}), flush=True)
    save(root / "automatic-details.json", details)
    return {"suite_hash": suite.suite_hash, "rows": rows}


def prepare(suite: EvaluationSuite, root: Path, entry: str, embedding_config: Path, maximum: int | None) -> None:
    backend = LocalMiniLM.from_file(embedding_config)
    plans = []
    for query in suite.corpus["queries"][:maximum]:
        started = time.perf_counter()
        prepared = seed(suite, root / query["id"], query["context_sources"], facts_enabled=True)
        config, runtime, owner = prepared["config"], prepared["runtime"], prepared["owner"]
        access = config.parent / ("mcp-access-" + entry + ".json")
        pair_client(config, client_id="synthetic-host-" + entry, entry=entry, access_path=access, scopes=("capture", "recall"))
        runtime.refresh_lexical(owner)
        semantic = SemanticRecall(runtime, backend)
        status = semantic.status(owner)
        while status.pending_messages:
            status = semantic.refresh(owner, limit=16, timeout=30)
        temporal = query.get("temporal")
        conditions = None
        if temporal:
            checkpoint = prepared["facts"].get(temporal["known_after"])
            conditions = {"subject": temporal["subject"], "predicate": temporal["predicate"], "valid_at": temporal["valid_at"],
                "known_after": temporal["known_after"], "known_at": checkpoint.recorded_at if checkpoint else None,
                "checkpoint_seeded": checkpoint is not None}
        plans.append({"query_id": query["id"], "category": query["category"], "question": query["question"],
            "temporal_conditions": conditions, "config": str(config), "access": str(access),
            "source_aliases": {value: key for key, value in prepared["ids"].items()}, "source_aliases_not_sent_to_model": True,
            "fact_keys": [list(key) for key in sorted({(row["subject"], row["predicate"]) for row in suite.corpus["facts"] if row["id"] in prepared["facts"]})],
            "seeded_facts": sorted(prepared["facts"]),
            "semantic": asdict(status), "fixture_seconds": time.perf_counter() - started})
        save(root / "host-plan.json", {"suite_hash": suite.suite_hash, "entry": entry, "plans": plans,
            "seed_scope": "Each query has an independent public-API fixture using only context_sources. Only complete allowed fact timelines are seeded; partial contexts use original evidence, not fabricated predecessor records. Core temporal scoring is separate."})
        print(json.dumps({"phase": "prepare", "entry": entry, "query_id": query["id"], "prepared": len(plans)}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("core", "automatic", "prepare"))
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-memory-v1"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--entry", choices=("dsh", "pi"))
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--max-queries", type=int)
    args = parser.parse_args()
    suite = load_suite(args.suite)
    root = external(args.output)
    if args.phase == "core":
        observations = run_core(suite, root)
    else:
        root.mkdir()
        save(root / "manifest.json", {"suite_hash": suite.suite_hash,
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "phase": args.phase})
        if args.phase == "prepare":
            if not args.entry or not args.embedding_config:
                raise ValueError("host_preparation_parameters_missing")
            if args.max_queries is not None and not 1 <= args.max_queries <= len(suite.corpus["queries"]):
                raise ValueError("invalid_fixture_limit")
            prepare(suite, root, args.entry, args.embedding_config, args.max_queries)
            return 0
        observations = automatic(suite, root)
    save(root / "observations.json", observations)
    report = score(suite, observations)
    save(root / "report.json", report)
    return 0 if report["outcome"] == "passed" else 1 if report["outcome"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
