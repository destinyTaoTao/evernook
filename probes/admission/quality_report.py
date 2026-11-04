"""Merge actual observations per host without filling missing or failed answers."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Any

from evernook.evaluation import load_suite, score
from evernook.paths import external
from scale_ingest import save


def distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {"samples": len(ordered), "p50_ms": ordered[math.ceil(len(ordered) * .5) - 1] if ordered else None,
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
            "max_ms": ordered[-1] if ordered else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--automatic", type=Path, required=True)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-memory-v1"))
    args = parser.parse_args()
    suite = load_suite(args.suite)
    destination = external(args.output)
    destination.mkdir()
    merged: dict[str, Any] = {"suite_hash": suite.suite_hash, "rows": []}
    for directory in (args.core, args.automatic, args.host):
        observations = json.loads((directory / "observations.json").read_text(encoding="utf-8"))
        if observations["suite_hash"] != suite.suite_hash:
            raise ValueError("suite_identity_mismatch")
        merged["rows"].extend(observations["rows"])
    result = score(suite, merged)
    save(destination / "observations.json", merged)
    save(destination / "scoring.json", result)
    queries = {row["id"]: row for row in suite.corpus["queries"]}
    cases = {case.id: case for case in suite.cases}
    groups: dict[str, Counter[str]] = {}
    for check in result["checks"]:
        case = cases[check["id"]]
        category = queries[case.query_id]["category"] if case.query_id is not None else "source_record"
        key = category + "/" + case.dimension
        groups.setdefault(key, Counter())[check["status"]] += 1
    runs = json.loads((args.host / "runs.json").read_text(encoding="utf-8"))
    latencies = {}
    for field in ("memory_pre_provider_ms", "first_reliable_visible_ms", "host_boot_ms", "request_ms"):
        measured = [row[field] for row in runs["runs"] if row[field] is not None and row[field] > 0]
        latencies[field] = {**distribution(measured), "unmeasured": len(runs["runs"]) - len(measured)}
    summary = {"entry": runs["entry"], "categories": groups, "metrics": result["metrics"],
        "blocking_failures": result["blocking_failures"], "latencies": latencies,
        "errors": Counter(row["error_code"] for row in runs["runs"] if row["error_code"]),
        "host_cases_observed": len(runs["runs"]), "network_attempts": runs["network_attempts"],
        "independent_replay_verified": sum(row["final_replay_matches"] is True and row["first_visible_independently_read"] is True for row in runs["runs"]),
        "admission": "blocked", "scope": "Quality and latency evidence only. This command never admits a model or activates any profile; scale and resource gates are separate."}
    save(destination / "summary.json", summary)
    return 0 if result["outcome"] == "passed" else 1 if result["outcome"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
