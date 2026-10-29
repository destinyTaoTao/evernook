"""Rebuild report statistics from preserved raw probe results without rerunning models."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"samples": len(ordered), "p50Ms": ordered[math.ceil(len(ordered) * .5) - 1],
            "p95Ms": ordered[math.ceil(len(ordered) * .95) - 1]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    root = parser.parse_args().run.resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Run evidence must be outside checkout")
    read = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
    if read("status.json") != {"ok": True, "networkAttempts": []}:
        raise ValueError("Offline run did not complete cleanly")
    rows = [row for row in read("measurements.json") if row["repeat"] > 0]
    lexical = [row for row in read("lexical-path.json")["measurements"] if row["repeat"] > 0]
    report = {}
    for engine in ("memos", "minimal"):
        samples = [row for row in rows if row["engine"] == engine]
        positive = [row for row in samples if row["expected"]]
        report[engine] = {
            "completeRequest": percentiles([row["durationMs"] for row in samples]),
            "lexicalCandidatesOnly": percentiles([row["durationMs"] for row in lexical if row["engine"] == engine]),
            "positiveQueryCount": len(positive) // 3,
            "hitAt10": statistics.mean(bool(set(row["ids"][:10]) & set(row["expected"])) for row in positive),
            "macroRecallAt10": statistics.mean(len(set(row["ids"][:10]) & set(row["expected"])) / len(row["expected"]) for row in positive),
            "hitAt1": statistics.mean(bool(row["ids"]) and row["ids"][0] in row["expected"] for row in positive),
        }
        if engine == "minimal":
            report[engine]["queryEmbedding"] = percentiles([row["embeddingMs"] for row in samples])
            report[engine]["fusionAndSourceRead"] = percentiles([row["pathMs"] for row in samples])
    bridge = read("bridge-results.json")
    report["bridge"] = {"startupMs": bridge["startupMs"],
                        "roundtrip": percentiles([row["roundtripMs"] for row in bridge["samples"]]),
                        "overhead": percentiles([row["overheadMs"] for row in bridge["samples"]])}
    report["datasetHash"] = read("manifest.json")["corpusHash"]
    (root / "derived-summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
