"""Frozen learning experiments: observations are evidence, never executable instructions."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .evaluation import EvaluationError, _canonical, _object, _parse, _raw, _strings, _timestamp

ARMS = ("raw", "observations", "views", "reflect")
PHASES = ("initial", "replay", "continued", "processor_changed", "restored")
DIMENSIONS = ("retrieval", "evidence", "judgment", "automatic", "maintenance", "host_answer")
VERSION = "evernook-learning-evaluation-v1"
Json = dict[str, Any]


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value) is not None


@dataclass(frozen=True)
class LearningSuite:
    root: Path
    manifest: Json
    sources: dict[str, Json]
    steps: tuple[Json, ...]
    cases: dict[str, Json]
    suite_hash: str


def _ids(value: object, allowed: set[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        raise EvaluationError("invalid_learning_reference")
    if len(set(value)) != len(value):
        raise EvaluationError("duplicate_learning_reference")
    return value


def load_suite(root: Path) -> LearningSuite:
    try:
        return _load(root)
    except EvaluationError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise EvaluationError("invalid_learning_suite") from None


def _load(root: Path) -> LearningSuite:
    manifest = _object(_parse(_raw(root / "manifest.json")))
    if manifest.get("scorer_version") != VERSION or manifest.get("arms") != list(ARMS) or manifest.get("phases") != list(PHASES):
        raise EvaluationError("unsupported_learning_version")
    if (not isinstance(manifest.get("forbidden_literals"), list)
            or any(not isinstance(item, str) or not item for item in manifest["forbidden_literals"])):
        raise EvaluationError("invalid_learning_forbidden_literals")
    assets = {}
    for filename in ("corpus.json", "expectations.json"):
        raw = _raw(root / filename)
        if hashlib.sha256(raw).hexdigest() != manifest.get(filename.replace(".json", "_sha256")):
            raise EvaluationError("learning_hash_mismatch")
        assets[filename] = _parse(raw)
    policy = _raw(root.parent.parent / "probes" / "admission" / "policy.json")
    if hashlib.sha256(policy).hexdigest() != manifest.get("policy_sha256"):
        raise EvaluationError("learning_policy_mismatch")
    corpus = _object(assets["corpus.json"])
    if set(corpus) != {"sources", "steps", "generated_sources"} or not isinstance(corpus["sources"], list) or not isinstance(corpus["steps"], list):
        raise EvaluationError("invalid_learning_corpus")
    if not isinstance(corpus["generated_sources"], list):
        raise EvaluationError("invalid_learning_generator")
    expanded = list(corpus["sources"])
    for value in corpus["generated_sources"]:
        generator = _object(value)
        if (set(generator) != {"prefix", "count", "text", "at"} or not _identifier(generator["prefix"])
                or type(generator["count"]) is not int or not 1 <= generator["count"] <= 20000
                or not isinstance(generator["text"], str) or not 0 < len(generator["text"]) <= 128
                or len(expanded) + generator["count"] > 25000):
            raise EvaluationError("invalid_learning_generator")
        _timestamp(generator["at"])
        expanded.extend({"id": f"{generator['prefix']}-{index}", "text": generator["text"] + str(index),
                         "role": "user", "at": generator["at"], "session": "generated-noise"} for index in range(generator["count"]))
    if hashlib.sha256(_canonical(expanded)).hexdigest() != manifest.get("expanded_sources_sha256"):
        raise EvaluationError("learning_expansion_mismatch")
    sources: dict[str, Json] = {}
    for value in expanded:
        source = _object(value)
        if (set(source) != {"id", "role", "text", "at", "session"} or not isinstance(source["id"], str)
                or not _identifier(source["id"]) or source["id"] in sources or source["role"] not in {"user", "assistant", "tool"}
                or not isinstance(source["text"], str) or not 0 < len(source["text"]) <= 16384
                or not isinstance(source["session"], str) or not source["session"]):
            raise EvaluationError("invalid_learning_source")
        _timestamp(source["at"])
        sources[source["id"]] = source
    cases: dict[str, Json] = {}
    active: dict[str, set[str]] = {}
    steps = []
    for value in corpus["steps"]:
        step = _object(value)
        if (set(step) != {"id", "scenario", "category", "phase", "add", "replay", "controls", "question", "as_of"}
                or any(not isinstance(step[key], str) or not step[key] for key in ("id", "scenario", "category", "question"))
                or step["id"] in cases or step["phase"] not in PHASES or not isinstance(step["controls"], list)):
            raise EvaluationError("invalid_learning_step")
        if not _identifier(step["id"]) or not _identifier(step["scenario"]):
            raise EvaluationError("invalid_learning_identifier")
        _timestamp(step["as_of"])
        known = active.setdefault(step["scenario"], set())
        additions = _ids(step["add"], set(sources))
        if known.intersection(additions):
            raise EvaluationError("learning_source_readded")
        known.update(additions)
        _ids(step["replay"], known)
        for control in step["controls"]:
            control = _object(control)
            if set(control) != {"source_id", "action", "evidence_id"} or control["action"] not in {"suppress", "revoke", "purge"}:
                raise EvaluationError("invalid_learning_control")
            _ids([control["source_id"], control["evidence_id"]], known)
        step = {**step, "available_sources": sorted(known)}
        cases[step["id"]] = step
        steps.append(step)
    expected = assets["expectations.json"]
    if not isinstance(expected, list) or len(expected) != len(cases):
        raise EvaluationError("invalid_learning_expectations")
    seen: set[str] = set()
    for value in expected:
        row = _object(value)
        if set(row) != {"case_id", "expected"} or row["case_id"] not in cases or row["case_id"] in seen:
            raise EvaluationError("invalid_learning_expectations")
        seen.add(row["case_id"])
        oracle = _object(row["expected"])
        if set(oracle) != set(DIMENSIONS):
            raise EvaluationError("invalid_learning_dimensions")
        allowed = set(cases[row["case_id"]]["available_sources"])
        retrieval = _object(oracle["retrieval"])
        if set(retrieval) != {"required", "forbidden"}:
            raise EvaluationError("invalid_learning_retrieval")
        required, forbidden = _ids(retrieval["required"], allowed), _ids(retrieval["forbidden"], allowed)
        if set(required).intersection(forbidden) or len(required) > 10:
            raise EvaluationError("invalid_learning_retrieval")
        evidence = _object(oracle["evidence"])
        if set(evidence) != {"required_quotes", "versions"} or set(evidence["required_quotes"]) != set(required) or set(evidence["versions"]) != set(required):
            raise EvaluationError("invalid_learning_evidence")
        for source_id in required:
            quote = evidence["required_quotes"][source_id]
            if not isinstance(quote, str) or not quote or quote not in sources[source_id]["text"] or evidence["versions"][source_id] != [1, 1]:
                raise EvaluationError("invalid_learning_evidence")
        for dimension in ("judgment", "automatic", "host_answer"):
            decision = _object(oracle[dimension])
            keys = {"decision", "value", "source_ids"} | ({"answer_text"} if dimension == "host_answer" else set())
            if set(decision) != keys or decision["decision"] not in {"answer", "abstain", "clarify", "refuse", "accept", "review"}:
                raise EvaluationError("invalid_learning_decision")
            if dimension == "host_answer" and (not isinstance(decision["answer_text"], str) or not decision["answer_text"]):
                raise EvaluationError("invalid_learning_answer")
            if decision["value"] is not None and not isinstance(decision["value"], str):
                raise EvaluationError("invalid_learning_decision")
            _ids(decision["source_ids"], allowed)
        maintenance = _object(oracle["maintenance"])
        if (set(maintenance) != {"independent_support", "readmitted"} or type(maintenance["independent_support"]) is not int
                or maintenance["independent_support"] < 0 or type(maintenance["readmitted"]) is not bool):
            raise EvaluationError("invalid_learning_maintenance")
        cases[row["case_id"]] = {**cases[row["case_id"]], "expected": oracle}
    if not cases or {case["phase"] for case in cases.values()} != set(PHASES):
        raise EvaluationError("missing_learning_phases")
    return LearningSuite(root, manifest, sources, tuple(steps), cases, hashlib.sha256(_canonical(manifest)).hexdigest())


def _evidence(suite: LearningSuite, case: Json, payload: Json) -> bool:
    if set(payload) != {"citations"} or not isinstance(payload["citations"], list):
        return False
    expected = case["expected"]["evidence"]
    quotes: dict[str, list[str]] = {}
    for value in payload["citations"]:
        citation = _object(value)
        if set(citation) != {"source_id", "start", "end", "quote", "state_version", "content_version"}:
            return False
        source_id = citation["source_id"]
        if not isinstance(source_id, str) or source_id not in expected["required_quotes"]:
            return False
        start, end = citation["start"], citation["end"]
        text = suite.sources[source_id]["text"]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or citation["quote"] != text[start:end]
                or type(citation["state_version"]) is not int or type(citation["content_version"]) is not int
                or [citation["state_version"], citation["content_version"]] != expected["versions"][source_id]):
            return False
        quotes.setdefault(source_id, []).append(citation["quote"])
    return all(any(quote in span for span in quotes.get(source_id, [])) for source_id, quote in expected["required_quotes"].items())


def _distribution(values: list[float]) -> Json:
    values = sorted(values)
    return {"samples": len(values), "p50_ms": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p95_ms": values[math.ceil(len(values) * .95) - 1] if values else None}


def score(suite: LearningSuite, observations: Json) -> Json:
    try:
        return _score(suite, observations)
    except EvaluationError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise EvaluationError("invalid_learning_observations") from None


def _score(suite: LearningSuite, observations: Json) -> Json:
    if set(observations) != {"suite_hash", "configuration", "rows"} or observations["suite_hash"] != suite.suite_hash:
        raise EvaluationError("learning_identity_mismatch")
    config = _object(observations["configuration"])
    if (set(config) != {"model", "processor", "budget", "policy_sha256", "implementation"}
            or config["policy_sha256"] != suite.manifest["policy_sha256"]
            or any(not isinstance(config[key], str) or not config[key] for key in ("model", "processor", "implementation"))
            or type(_object(config["budget"]).get("candidate_k")) is not int or config["budget"]["candidate_k"] != 10):
        raise EvaluationError("learning_configuration_mismatch")
    if not isinstance(observations["rows"], list):
        raise EvaluationError("invalid_learning_rows")
    rows: dict[tuple[str, str], Json] = {}
    latency: dict[str, list[float]] = {arm: [] for arm in ARMS}
    phase_latency: dict[str, dict[str, list[float]]] = {arm: {phase: [] for phase in PHASES} for arm in ARMS}
    private: list[int] = []
    children: list[int] = []
    for value in observations["rows"]:
        row = _object(value)
        if set(row) != {"case_id", "arm", "payload", "measurements"} or row["case_id"] not in suite.cases or row["arm"] not in ARMS:
            raise EvaluationError("invalid_learning_result_id")
        key = (row["arm"], row["case_id"])
        if key in rows:
            raise EvaluationError("duplicate_learning_result")
        payload = _object(row["payload"])
        if set(payload) - set(DIMENSIONS):
            raise EvaluationError("invalid_learning_dimension")
        if "retrieval" in payload:
            if set(_object(payload["retrieval"])) != {"source_ids"}:
                raise EvaluationError("invalid_learning_retrieval")
            _ids(payload["retrieval"]["source_ids"], set(suite.cases[row["case_id"]]["available_sources"]))
        measurements = _object(row["measurements"])
        if set(measurements) - {"elapsed_ms", "private_bytes", "remaining_children"}:
            raise EvaluationError("invalid_learning_measurement")
        for name, measurement in measurements.items():
            if measurement is None:
                continue
            if type(measurement) not in (float, int) or not math.isfinite(measurement) or measurement < 0:
                raise EvaluationError("invalid_learning_measurement")
            if name == "elapsed_ms":
                latency[row["arm"]].append(float(measurement))
                phase_latency[row["arm"]][suite.cases[row["case_id"]]["phase"]].append(float(measurement))
            else:
                if type(measurement) is not int:
                    raise EvaluationError("invalid_learning_measurement")
                (private if name == "private_bytes" else children).append(measurement)
        rows[key] = row
    arms: Json = {}
    phases: Json = {}
    recalls: Json = {}
    coverage: Json = {}
    automatic: Json = {}
    categories: Json = {}
    results = []
    totals = {"passed": 0, "failed": 0, "missing": 0}
    for arm in ARMS:
        arms[arm] = {dimension: {"expected": len(suite.cases), "passed": 0, "failed": 0, "missing": 0} for dimension in DIMENSIONS}
        phases[arm] = {phase: {dimension: {"passed": 0, "failed": 0, "missing": 0} for dimension in DIMENSIONS} for phase in PHASES}
        recall: list[float] = []
        accepted, wrong_accepts = 0, 0
        categories[arm] = {}
        for case_id, case in suite.cases.items():
            category = categories[arm].setdefault(case["category"], {
                dimension: {"passed": 0, "failed": 0, "missing": 0} for dimension in DIMENSIONS})
            payload = rows.get((arm, case_id), {}).get("payload", {})
            for dimension in DIMENSIONS:
                state = "missing"
                if dimension in payload:
                    actual = _object(payload[dimension])
                    if dimension == "retrieval":
                        found = actual["source_ids"][:10]
                        expected = case["expected"][dimension]
                        required = set(expected["required"])
                        recall.append(len(required.intersection(found)) / len(required) if required else float(not found))
                        passed = (required.issubset(found) and not set(expected["forbidden"]).intersection(found)
                                  and (bool(required) or not found))
                    elif dimension == "evidence":
                        passed = _evidence(suite, case, actual)
                    elif dimension == "host_answer":
                        answer = {key: value for key, value in actual.items() if key != "citations"}
                        passed = (_canonical(answer) == _canonical(case["expected"][dimension]) and "citations" in actual
                                  and _evidence(suite, case, {"citations": actual["citations"]}))
                    else:
                        passed = _canonical(actual) == _canonical(case["expected"][dimension])
                    # Original evidence can contain a synthetic secret or injection. Outputs must not disclose it.
                    if dimension in {"judgment", "automatic", "host_answer"} and any(
                            literal in text for literal in suite.manifest["forbidden_literals"] for text in _strings(actual)):
                        passed = False
                    state = "passed" if passed else "failed"
                    if dimension == "automatic" and actual.get("decision") == "accept":
                        accepted += 1
                        wrong_accepts += int(not passed)
                arms[arm][dimension][state] += 1
                phases[arm][case["phase"]][dimension][state] += 1
                category[dimension][state] += 1
                totals[state] += 1
                results.append({"case_id": case_id, "arm": arm, "phase": case["phase"], "dimension": dimension, "state": state})
        recalls[arm] = sum(recall) / len(recall) if recall else None
        coverage[arm] = {"observed": len(recall), "expected": len(suite.cases), "fraction": len(recall) / len(suite.cases)}
        automatic[arm] = {"observed_accepts": accepted, "incorrect_accepts": wrong_accepts,
                          "false_update_rate": wrong_accepts / accepted if accepted else None}
    return {"suite_hash": suite.suite_hash, "scorer_version": VERSION,
            "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "configuration": config, "configuration_sha256": hashlib.sha256(_canonical(config)).hexdigest(),
            "outcome": "failed" if totals["failed"] else "incomplete" if totals["missing"] else "passed",
            "admission": "pending", "totals": totals, "arms": arms, "phases": phases, "categories": categories,
            "candidate_recall_at_10": recalls, "retrieval_coverage": coverage, "automatic_updates": automatic,
            "latency": {arm: _distribution(values) for arm, values in latency.items()},
            "phase_latency": {arm: {phase: _distribution(values) for phase, values in stages.items()} for arm, stages in phase_latency.items()},
            "resources": {"peak_private_bytes": max(private) if private else None, "remaining_children": max(children) if children else None},
            "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "score"))
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-learning-v1"))
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        suite = load_suite(args.suite)
        if args.operation == "validate":
            print(json.dumps({"suite_hash": suite.suite_hash, "cases": len(suite.cases), "admission": "pending"}))
            return 0
        if args.observations is None:
            parser.error("--observations required for score")
        report = score(suite, _object(_parse(_raw(args.observations))))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["outcome"] == "failed" else 2 if report["outcome"] == "incomplete" else 0
    except EvaluationError as failure:
        print(json.dumps({"error_code": str(failure)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
