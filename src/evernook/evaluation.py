"""Offline, hash-bound evaluation of public observations; never runs retrieved instructions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

SCORER_VERSION = "evernook-evaluation-v1"
DIMENSIONS = ("storage", "retrieval", "temporal", "auto_acceptance", "host_answer")
JsonObject = dict[str, Any]


class EvaluationError(ValueError):
    """A bounded error code, without echoing observation content."""


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    dimension: str
    query_id: str | None
    blocking: bool
    expected: JsonObject


@dataclass(frozen=True)
class EvaluationSuite:
    root: Path
    manifest: JsonObject
    corpus: JsonObject
    cases: tuple[EvaluationCase, ...]
    suite_hash: str
    policy_hash: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _policy_hash(manifest: JsonObject) -> str:
    return hashlib.sha256(_canonical({key: manifest[key] for key in ("forbidden_literals", "model_hardware_gate")})).hexdigest()


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise EvaluationError("invalid_evaluation_object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("duplicate_evaluation_key")
        result[key] = value
    return result


def _raw(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise EvaluationError("evaluation_file_too_large")
        return raw
    except OSError:
        raise EvaluationError("invalid_evaluation_file") from None


def _invalid_number(_: str) -> None:
    raise EvaluationError("invalid_evaluation_number")


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_number)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise EvaluationError("invalid_evaluation_file") from None


def _read(path: Path) -> Any:
    return _parse(_raw(path))


def load_suite(root: Path) -> EvaluationSuite:
    try:
        return _load_suite(root)
    except EvaluationError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise EvaluationError("invalid_evaluation_suite") from None


def _load_suite(root: Path) -> EvaluationSuite:
    manifest = _object(_read(root / "manifest.json"))
    version = {key: manifest.get(key) for key in ("suite_version", "scorer_version")}
    if not isinstance(version["suite_version"], str) or version["scorer_version"] != SCORER_VERSION:
        raise EvaluationError("unsupported_evaluation_version")
    if hashlib.sha256(_canonical(version)).hexdigest() != manifest.get("version_sha256"):
        raise EvaluationError("evaluation_hash_mismatch")
    assets = {}
    for filename, key in (("corpus.json", "corpus_sha256"), ("expectations.json", "expectations_sha256")):
        assets[filename] = _raw(root / filename)
        digest = hashlib.sha256(assets[filename]).hexdigest()
        if digest != manifest.get(key):
            raise EvaluationError("evaluation_hash_mismatch")
    corpus = _object(_parse(assets["corpus.json"]))
    if set(corpus) != {"sources", "facts", "queries"} or any(not isinstance(value, list) for value in corpus.values()):
        raise EvaluationError("invalid_evaluation_corpus")
    source_ids = _ids(corpus["sources"])
    query_ids = _ids(corpus["queries"])
    fact_ids = _ids(corpus["facts"])
    questions: set[str] = set()
    for source in corpus["sources"]:
        if (not {"id", "role", "text", "at"}.issubset(source) or set(source) - {"id", "role", "text", "at", "session"}
                or ("session" in source and not isinstance(source["session"], str)) or source["role"] not in {"user", "assistant"}
                or not isinstance(source["text"], str) or not 0 < len(source["text"]) <= 16384):
            raise EvaluationError("invalid_evaluation_source")
        _timestamp(source["at"])
    seen_facts: set[str] = set()
    for fact in corpus["facts"]:
        if (set(fact) != {"id", "source_id", "subject", "predicate", "kind", "valid_from", "replaces", "resolves"}
                or fact["source_id"] not in source_ids or fact["kind"] not in {"assert", "change", "correction", "plan", "disputed"}
                or not all(isinstance(fact[key], str) and fact[key] for key in ("subject", "predicate"))
                or (fact["replaces"] is not None and fact["replaces"] not in seen_facts)
                or not isinstance(fact["resolves"], list) or any(item not in seen_facts for item in fact["resolves"])):
            raise EvaluationError("invalid_evaluation_fact")
        if fact["valid_from"] is not None:
            _timestamp(fact["valid_from"])
        seen_facts.add(fact["id"])
    for query in corpus["queries"]:
        if (not {"id", "question", "category"}.issubset(query) or set(query) - {"id", "question", "category", "retrieval", "temporal", "group", "context_sources"}
                or not isinstance(query["question"], str) or not query["question"] or query["question"] in questions
                or not isinstance(query["category"], str) or not query["category"]):
            raise EvaluationError("invalid_evaluation_query")
        questions.add(query["question"])
        if "context_sources" in query and (not isinstance(query["context_sources"], list) or any(item not in source_ids for item in query["context_sources"])):
            raise EvaluationError("invalid_evaluation_context")
        if "retrieval" in query:
            retrieval = _object(query["retrieval"])
            if set(retrieval) != {"query", "mode"} or retrieval["mode"] not in {"exact", "lexical"} or not isinstance(retrieval["query"], str) or not 0 < len(retrieval["query"]) <= 256:
                raise EvaluationError("invalid_evaluation_query")
        if "temporal" in query:
            temporal = _object(query["temporal"])
            if set(temporal) != {"subject", "predicate", "valid_at", "known_after"} or not all(isinstance(temporal[key], str) for key in ("subject", "predicate")):
                raise EvaluationError("invalid_evaluation_query")
            _timestamp(temporal["valid_at"])
            if temporal["known_after"] is not None and temporal["known_after"] not in fact_ids:
                raise EvaluationError("invalid_evaluation_query")
    raw_cases = _parse(assets["expectations.json"])
    if not isinstance(raw_cases, list):
        raise EvaluationError("invalid_evaluation_cases")
    cases = []
    ids: set[str] = set()
    for raw in raw_cases:
        row = _object(raw)
        if (set(row) != {"id", "dimension", "query_id", "blocking", "expected"}
                or not isinstance(row["id"], str) or not row["id"] or row["id"] in ids
                or row["dimension"] not in DIMENSIONS or type(row["blocking"]) is not bool
                or (row["query_id"] is not None and not isinstance(row["query_id"], str))):
            raise EvaluationError("invalid_evaluation_case")
        ids.add(row["id"])
        if row["query_id"] is not None and row["query_id"] not in query_ids:
            raise EvaluationError("invalid_evaluation_query_reference")
        expected = _object(row["expected"])
        _validate_expected(row["dimension"], expected, source_ids, fact_ids)
        if row["dimension"] in {"retrieval", "temporal"}:
            query = next((item for item in corpus["queries"] if item["id"] == row["query_id"]), {})
            if row["dimension"] not in query:
                raise EvaluationError("invalid_evaluation_query_reference")
        cases.append(EvaluationCase(row["id"], row["dimension"], row["query_id"], row["blocking"], expected))
    if (not isinstance(manifest.get("forbidden_literals"), list)
            or any(not isinstance(item, str) or not item for item in manifest["forbidden_literals"])
            or manifest.get("model_hardware_gate") != "pending"):
        raise EvaluationError("invalid_evaluation_gate")
    review = _object(manifest.get("human_review"))
    if review.get("state") not in {"pending", "attested"} or not isinstance(review.get("query_ids"), list):
        raise EvaluationError("invalid_evaluation_review")
    policy_hash = _policy_hash(manifest)
    identity = {**version, **{key: manifest[key] for key in ("corpus_sha256", "expectations_sha256", "version_sha256")},
                "policy_sha256": policy_hash}
    suite_hash = hashlib.sha256(_canonical(identity)).hexdigest()
    if review["state"] == "attested" and (not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or len(review["query_ids"]) != len(query_ids) or set(review["query_ids"]) != query_ids or review.get("suite_hash") != suite_hash):
        raise EvaluationError("incomplete_evaluation_review")
    return EvaluationSuite(root.resolve(), manifest, corpus, tuple(cases), suite_hash, policy_hash)


def _ids(rows: list[Any]) -> set[str]:
    result: set[str] = set()
    for raw in rows:
        row = _object(raw)
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in result:
            raise EvaluationError("invalid_evaluation_id")
        result.add(row["id"])
    return result


def _validate_expected(dimension: str, expected: JsonObject, source_ids: set[str], fact_ids: set[str]) -> None:
    keys = {"storage": {"source_id", "text", "original_saved"}, "retrieval": {"source_ids"},
            "temporal": {"state", "fact_id", "text", "disputes", "plans"},
            "auto_acceptance": {"decision", "value", "source_ids"}, "host_answer": {"decision", "answer", "source_ids"}}
    if set(expected) != keys[dimension]:
        raise EvaluationError("invalid_evaluation_expectation")
    for key in ("source_ids", "disputes", "plans"):
        if key in expected:
            values = expected[key]
            allowed = source_ids if key == "source_ids" else fact_ids
            if (not isinstance(values, list) or any(not isinstance(value, str) or value not in allowed for value in values)
                    or len(set(values)) != len(values) or (dimension == "retrieval" and len(values) > 10)):
                raise EvaluationError("invalid_evaluation_expectation")
    if dimension == "storage":
        if (expected["source_id"] not in source_ids or type(expected["original_saved"]) is not bool
                or (expected["original_saved"] and not isinstance(expected["text"], str))
                or (not expected["original_saved"] and expected["text"] is not None)):
            raise EvaluationError("invalid_evaluation_expectation")
    elif dimension == "temporal":
        if (expected["state"] not in {"current", "historical", "no_fact", "unknown_time", "disputed", "source_unavailable"}
                or (expected["fact_id"] is not None and expected["fact_id"] not in fact_ids)
                or (expected["text"] is not None and not isinstance(expected["text"], str))):
            raise EvaluationError("invalid_evaluation_expectation")
    elif dimension in {"auto_acceptance", "host_answer"}:
        decision = expected["decision"]
        allowed_decisions = {"accept", "review", "reject", "clarify"} if dimension == "auto_acceptance" else {"answer", "abstain", "clarify", "refuse"}
        value = expected["value" if dimension == "auto_acceptance" else "answer"]
        positive = "accept" if dimension == "auto_acceptance" else "answer"
        if (decision not in allowed_decisions or (decision == positive and (not isinstance(value, str) or not value))
                or (decision != positive and (value is not None or expected["source_ids"]))):
            raise EvaluationError("invalid_evaluation_expectation")


def _timestamp(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        raise EvaluationError("invalid_evaluation_timestamp") from None


def coverage(suite: EvaluationSuite) -> JsonObject:
    dates = [_timestamp(source["at"]) for source in suite.corpus["sources"]]
    groups = {fact["subject"] for fact in suite.corpus["facts"] if fact["kind"] in {"correction", "disputed"}}
    categories = sorted({query["category"] for query in suite.corpus["queries"]})
    return {"queries": len(suite.corpus["queries"]), "sources": len(suite.corpus["sources"]), "facts": len(suite.corpus["facts"]),
            "correction_or_conflict_groups": len(groups), "categories": categories,
            "earliest": min(dates).isoformat() if dates else None, "latest": max(dates).isoformat() if dates else None,
            "span_days": (max(dates) - min(dates)).days if dates else 0}


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)


def score(suite: EvaluationSuite, observations: JsonObject) -> JsonObject:
    try:
        if len(_canonical(observations)) > 16 * 1024 * 1024:
            raise EvaluationError("evaluation_file_too_large")
        return _score(suite, observations)
    except EvaluationError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise EvaluationError("invalid_evaluation_results") from None


def _score(suite: EvaluationSuite, observations: JsonObject) -> JsonObject:
    if (set(observations) != {"suite_hash", "rows"} or observations["suite_hash"] != suite.suite_hash
            or _policy_hash(suite.manifest) != suite.policy_hash):
        raise EvaluationError("evaluation_result_identity_mismatch")
    raw_rows = observations["rows"]
    if not isinstance(raw_rows, list):
        raise EvaluationError("invalid_evaluation_results")
    expected = {case.id: case for case in suite.cases}
    rows: dict[str, JsonObject] = {}
    for raw in raw_rows:
        row = _object(raw)
        if set(row) != {"id", "payload"} or not isinstance(row["id"], str) or row["id"] not in expected or row["id"] in rows:
            raise EvaluationError("invalid_evaluation_result_id")
        rows[row["id"]] = _object(row["payload"])
    metrics: JsonObject = {dimension: {"expected": 0, "passed": 0, "failed": 0, "missing": 0} for dimension in DIMENSIONS}
    checks = []
    blocking = []
    for case in suite.cases:
        metric = metrics[case.dimension]
        metric["expected"] += 1
        payload = rows.get(case.id)
        leak = payload is not None and any(literal in "".join(_strings(payload)) for literal in suite.manifest["forbidden_literals"])
        status = "missing" if payload is None else "passed" if _matches(suite, case, payload) and not leak else "failed"
        metric[status] += 1
        if status == "failed" and (case.blocking or leak):
            blocking.append(case.id)
        checks.append({"id": case.id, "dimension": case.dimension, "status": status})
    failed = any(metric["failed"] for metric in metrics.values())
    missing = any(metric["missing"] for metric in metrics.values())
    return {"suite_hash": suite.suite_hash, "policy_sha256": suite.policy_hash, "suite_version": suite.manifest["suite_version"], "scorer_version": SCORER_VERSION,
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "outcome": "failed" if failed else "incomplete" if missing else "passed",
            "admission": "pending", "human_review": suite.manifest["human_review"],
            "model_hardware_gate": "pending", "metrics": metrics, "blocking_failures": blocking, "checks": checks}


def _matches(suite: EvaluationSuite, case: EvaluationCase, payload: JsonObject) -> bool:
    if set(payload) != set(case.expected):
        return False
    actual = dict(payload)
    wanted = dict(case.expected)
    if case.dimension == "storage" and wanted.get("text") is None:
        original = next((source["text"] for source in suite.corpus["sources"] if source["id"] == wanted.get("source_id")), None)
        if type(actual.get("original_saved")) is not bool or actual["original_saved"] or not isinstance(actual.get("text"), str) or actual["text"] == original:
            return False
        actual["text"] = None
    for key in ("source_ids", "disputes", "plans"):
        if key in wanted:
            values = actual[key]
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
                return False
            actual[key], wanted[key] = sorted(values), sorted(wanted[key])
    return _canonical(actual) == _canonical(wanted)


def run_core(suite: EvaluationSuite, output: Path) -> JsonObject:
    """Run only implemented core dimensions in a new external synthetic ledger."""
    from .models import Event, Source
    from .runtime import MemoryRuntime, initialize
    from .candidates import CandidateDraft, CandidateWorkflow, EvidenceSpan
    from .facts import FactDecision, FactRecord, FactRegistry

    destination = output.resolve()
    if destination.exists():
        raise EvaluationError("evaluation_destination_exists")
    if any((parent / ".git").exists() for parent in (destination, *destination.parents)):
        raise EvaluationError("evaluation_output_inside_checkout")
    destination.mkdir(parents=True)
    config, access = initialize(destination / "runtime", client_id="evaluation", entry="evaluation")
    runtime = MemoryRuntime(config)
    ids = {}
    for index, source in enumerate(suite.corpus["sources"]):
        receipt = runtime.submit(access, Event(Source("evaluation", source.get("session", "synthetic-suite"), "main", source["id"]),
            source["role"], index, source["at"], "complete", source["text"]))
        ids[source["id"]] = receipt.message_id
    aliases = {value: key for key, value in ids.items()}
    workflow = CandidateWorkflow(runtime)
    registry = FactRegistry(runtime)
    facts: dict[str, FactRecord] = {}
    revisions: dict[tuple[str, str], int] = {}
    sources = {source["id"]: source for source in suite.corpus["sources"]}
    for fact in suite.corpus["facts"]:
        source_id = fact["source_id"]
        text = sources[source_id]["text"]
        lease = workflow.begin(access, (ids[source_id],), processor_version="synthetic-evaluation-v1")
        if lease.token is None:
            raise EvaluationError("evaluation_seed_failed")
        span = EvidenceSpan(ids[source_id], 1, "original", 1, 0, len(text), text)
        workflow.complete(access, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement", (span,)),))
        candidate = workflow.candidates(access, lease.status.run_id)[0]
        scope = (fact["subject"], fact["predicate"])
        revision = revisions.get(scope, 0)
        facts[fact["id"]] = registry.decide(access, FactDecision(fact["id"], *scope, candidate.candidate_id,
            ids[source_id], revision, fact["kind"], fact["valid_from"],
            replaces_id=facts[fact["replaces"]].fact_id if fact["replaces"] else None,
            resolves=tuple(facts[item].fact_id for item in fact["resolves"])))
        revisions[scope] = revision + 1
    fact_aliases = {record.fact_id: alias for alias, record in facts.items()}
    queries = {query["id"]: query for query in suite.corpus["queries"]}
    rows = []
    for case in suite.cases:
        if case.dimension == "storage":
            source_id = case.expected["source_id"]
            message = runtime.read(access, ids[source_id])
            payload: JsonObject = {"source_id": source_id, "original_saved": bool(message and message.receipt.original_saved),
                                   "text": message.event.text if message else None}
        elif case.dimension == "retrieval":
            query = queries[case.query_id]["retrieval"]
            hits = runtime.search(access, query["query"], mode=query["mode"], limit=10).hits
            payload = {"source_ids": [aliases.get(hit.message_id, "unmapped-source") for hit in hits]}
        elif case.dimension == "temporal":
            temporal = queries[case.query_id]["temporal"]
            view = registry.query(access, temporal["subject"], temporal["predicate"], valid_at=temporal["valid_at"],
                                  known_at=facts[temporal["known_after"]].recorded_at if temporal["known_after"] else None)
            payload = {"state": view.state, "fact_id": fact_aliases[view.fact.fact_id] if view.fact else None,
                       "text": view.fact.text if view.fact else None,
                       "disputes": [fact_aliases[item.fact_id] for item in view.disputes],
                       "plans": [fact_aliases[item.fact_id] for item in view.plans]}
        else:
            continue
        rows.append({"id": case.id, "payload": payload})
    result = {"suite_hash": suite.suite_hash, "rows": rows}
    (destination / "core-results.json").write_bytes(_canonical(result) + b"\n")
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from .models import MemoryFailure

    parser = argparse.ArgumentParser(description="Offline synthetic memory evaluation; no model admission.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "run-core", "score"):
        child = subparsers.add_parser(command)
        child.add_argument("--suite", required=True, type=Path)
        if command == "run-core":
            child.add_argument("--output", required=True, type=Path)
        if command == "score":
            child.add_argument("--results", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite)
        if args.command == "validate":
            result: JsonObject = {"suite_hash": suite.suite_hash, "policy_sha256": suite.policy_hash, "coverage": coverage(suite),
                "human_review": suite.manifest["human_review"], "model_hardware_gate": "pending"}
            exit_code = 0
        else:
            observations = run_core(suite, args.output) if args.command == "run-core" else _object(_read(args.results))
            report = score(suite, observations)
            result = report
            if args.command == "run-core":
                report_path = args.output.resolve() / "report.json"
                report_path.write_bytes(_canonical(report) + b"\n")
                result = {key: report[key] for key in ("suite_hash", "outcome", "admission", "metrics")}
                result["report"] = str(report_path)
            exit_code = 1 if report["outcome"] == "failed" else 2 if report["outcome"] == "incomplete" else 0
    except (EvaluationError, MemoryFailure) as error:
        result, exit_code = {"error": str(error)}, 1
    except OSError:
        result, exit_code = {"error": "evaluation_io_failed"}, 1
    sys.stdout.buffer.write(_canonical(result) + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
