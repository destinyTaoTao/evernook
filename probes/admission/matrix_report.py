"""Summarize actual matrix receipts without replacing missing or invalid samples."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from evernook.paths import external
from quality_report import distribution
from scale_ingest import save


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.matrix)
    destination = external(args.output)
    policy_raw = Path(__file__).with_name("policy.json").read_bytes()
    policy = json.loads(policy_raw)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["policy_sha256"] != hashlib.sha256(policy_raw).hexdigest():
        raise ValueError("policy_identity_mismatch")
    finished = (root / "report.json").exists()
    receipt = json.loads((root / ("report.json" if finished else "progress.json")).read_text(encoding="utf-8"))
    recorded = {row["result"]: row for row in receipt["results"]}
    cells: list[dict[str, Any]] = []
    for condition in policy["scale"]["conditions"]:
        for query in policy["scale"]["cells"]:
            for cache in ("warm", "fresh_process"):
                expected = policy["scale"]["warm_samples_per_cell" if cache == "warm" else "fresh_process_samples_per_cell"]
                samples: list[dict[str, Any]] = []
                valid: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                missing = 0
                for repetition in range(1 if cache == "warm" else expected):
                    name = f"{condition}-{query}-{cache}-{repetition}.json"
                    attempt = recorded.get(name)
                    if attempt is None:
                        missing += expected if cache == "warm" else 1
                        continue
                    path = root / name
                    rows = json.loads(path.read_text(encoding="utf-8"))["samples"] if path.exists() else []
                    samples.extend(rows)
                    missing += max(0, (expected if cache == "warm" else 1) - len(rows))
                    if attempt["returncode"] == 0 and attempt["valid_workload"]:
                        valid.extend(rows)
                    else:
                        failures.append(attempt)
                phases = ("recall_total_ms", "embedding_inclusive_ms", "retrieval_fusion_source_excluding_embedding_ms")
                timings = {field: distribution([row[field] for row in samples]) for field in ("lexical_ms", "context_ms")}
                timings.update({field: distribution([row["recall_phases"][field] for row in samples if row["recall_phases"] is not None]) for field in phases})
                populations = [row["lexical"]["index"]["source_messages"] for row in samples]
                complete = sum(row["context"]["complete"] is True for row in valid)
                cells.append({"condition": condition, "query": query, "cache": cache,
                    "expected_samples": expected, "observed_samples": len(samples), "missing_samples": missing,
                    "valid_workload_samples": len(valid), "valid_complete_contexts": complete,
                    "failed_or_invalid_attempts": failures, "timings_all_observed": timings,
                    "source_messages_min": min(populations) if populations else None,
                    "source_messages_max": max(populations) if populations else None,
                    "degraded": Counter(reason for row in samples for reason in row["context"]["degraded"]),
                    "complete_memory_p95_gate": complete == expected and len(valid) == expected
                        and timings["context_ms"]["p95_ms"] is not None
                        and timings["context_ms"]["p95_ms"] <= policy["latency_ms"]["complete_memory_p95"]})
    save(destination, {"matrix_finished": finished, "manifest": manifest, "cells": cells,
        "traffic": receipt["traffic"], "after_messages": receipt.get("after_messages"),
        "semantic_after": receipt.get("semantic_after"), "admission": "blocked",
        "scope": "Elapsed calls include incomplete coverage. Invalid attempts and missing samples remain explicit; diagnostics and unscored warmups are excluded. Partial active batches without a completed attempt receipt are missing here. Admission also requires separate quality, coverage, latency and resource gates."})
    return 0 if finished and all(cell["complete_memory_p95_gate"] for cell in cells) else 2


if __name__ == "__main__":
    raise SystemExit(main())
