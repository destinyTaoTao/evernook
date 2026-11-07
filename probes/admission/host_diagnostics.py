"""Explain frozen host-score failures without changing observations or the scorer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from evernook.evaluation import EvaluationSuite, load_suite, score
from evernook.paths import external


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def selected_source_ids(requests: list[Any]) -> set[str] | None:
    """Inspect dsh runtime-context sections; raw user JSON cannot establish provenance."""
    selected: set[str] = set()
    available = False
    pending = list(requests)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            source = value.get("source")
            if isinstance(source, dict) and source.get("kind") == "runtime-context":
                for section in source.get("sections", []):
                    if not isinstance(section, dict) or section.get("name") != "evernook-evidence":
                        continue
                    try:
                        payload = json.loads(section["text"])
                    except (KeyError, TypeError, ValueError, RecursionError):
                        continue
                    if not isinstance(payload, dict) or payload.get("kind") != "untrusted_memory_context":
                        continue
                    available = True
                    items = [payload.get("items", [])]
                    while items:
                        item = items.pop()
                        if isinstance(item, dict):
                            selected.update(v for k, v in item.items() if k in {"message_id", "evidence_id"} and isinstance(v, str))
                            items.extend(item.values())
                        elif isinstance(item, list):
                            items.extend(item)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return selected if available else None


def diagnose(suite: EvaluationSuite, observations: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    scored = score(suite, observations)
    checks = {row["id"]: row for row in scored["checks"]}
    expected = {case.id: case.expected for case in suite.cases if case.dimension == "host_answer"}
    by_query: dict[str, dict[str, Any]] = {}
    for run in runs:
        query = run["query_id"]
        if not isinstance(query, str) or query in by_query:
            raise ValueError("duplicate_or_invalid_host_run")
        by_query[query] = run
    details = []
    for row in observations["rows"]:
        case = row["id"]
        if case not in expected or case.removeprefix("host-") not in by_query:
            raise ValueError("host_diagnostic_identity_mismatch")
        actual, wanted = row["payload"], expected[case]
        different = []
        for key in sorted(set(actual) | set(wanted)):
            left, right = actual.get(key), wanted.get(key)
            if key == "source_ids" and isinstance(left, list) and all(isinstance(v, str) for v in left) and len(set(left)) == len(left):
                left = sorted(left)
                right = sorted(right) if isinstance(right, list) else right
            if key not in actual or key not in wanted or canonical(left) != canonical(right):
                different.append(key)
        answer, desired = actual.get("answer"), wanted["answer"]
        relation = "unavailable" if "answer" not in actual else "equal" if canonical(answer) == canonical(desired) else "different"
        if isinstance(answer, str) and isinstance(desired, str) and answer != desired:
            relation = "expected_inside_observed" if desired in answer else "observed_inside_expected" if answer in desired else "different"
        run = by_query[case.removeprefix("host-")]
        current = (run.get("context") or {}).get("latest_input", {}).get("message_id")
        try:
            visible = json.loads(run["visible"])
            cited = visible.get("source_ids", []) if isinstance(visible, dict) else None
        except (ValueError, RecursionError):
            cited = None
        selected = selected_source_ids(run.get("requests", []))
        details.append({"id": case, "status": checks[case]["status"], "different_fields": different,
            "answer_relation": relation, "current_input_receipt_available": isinstance(current, str),
            "current_input_cited": current in cited if isinstance(current, str) and isinstance(cited, list) else None,
            "current_input_selected_as_memory": current in selected if isinstance(current, str) and selected is not None else None,
            "first_visible_independently_read": run.get("first_visible_independently_read"),
            "final_replay_matches": run.get("final_replay_matches"), "error_code": run.get("error_code")})
    if len(details) != len(by_query):
        raise ValueError("host_diagnostic_run_without_observation")
    return {"suite_hash": suite.suite_hash, "score": scored, "details": details,
        "scope": "Mechanical field/literal/source comparisons only; no semantic correctness verdict or admission."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-memory-v1"))
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    suite = load_suite(args.suite)
    rows, runs, inputs = [], [], []
    for path in args.input:
        root = external(path)
        captured = {}
        for name in ("observations.json", "runs.json"):
            with (root / name).open("rb") as stream:
                content = stream.read(64 * 1024 * 1024 + 1)
            if len(content) > 64 * 1024 * 1024:
                raise ValueError("diagnostic_input_too_large")
            captured[name] = json.loads(content)
            inputs.append({"name": name, "sha256": hashlib.sha256(content).hexdigest()})
        if captured["observations.json"]["suite_hash"] != suite.suite_hash:
            raise ValueError("host_diagnostic_suite_mismatch")
        rows.extend(captured["observations.json"]["rows"])
        runs.extend(captured["runs.json"]["runs"])
    report = diagnose(suite, {"suite_hash": suite.suite_hash, "rows": rows}, runs)
    report["inputs"] = inputs
    report["diagnostic_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    target = external(args.output)
    target.mkdir()
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
