"""Bounded real MiniLM I/O regression; pinned assets and outputs stay outside checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import evernook.embedding as embedding
from evernook import LocalMiniLM
from evernook.paths import external


TEXTS = ("合成：目前使用Java21。", "合成：饮食偏好是什么？", "🙂" * 256)


def run(config: Path, output: Path, baseline: Path | None) -> None:
    output = external(output)
    output.mkdir(parents=True, exist_ok=False)
    if os.environ.get("NODE_OPTIONS") or os.environ.get("EVERNOOK_EMBEDDING_READ_TRACE"):
        raise ValueError("preexisting_worker_trace_environment")
    backend = LocalMiniLM.from_file(config)
    trace = output / "graph-reads.jsonl"
    records: list[dict[str, Any]] = []
    os.environ["NODE_OPTIONS"] = "--import=" + Path(__file__).with_name("embedding_reads.mjs").resolve().as_uri()
    os.environ["EVERNOOK_EMBEDDING_READ_TRACE"] = str(trace)
    try:
        for index in range(3):
            started = time.perf_counter()
            vectors = backend.embed(TEXTS, timeout=15)
            records.append({"index": index, "elapsed_ms": (time.perf_counter() - started) * 1000,
                            "vectors": vectors})
    finally:
        os.environ.pop("NODE_OPTIONS", None)
        os.environ.pop("EVERNOOK_EMBEDDING_READ_TRACE", None)
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    observed = sum(row["bytes_read"] for row in events if row["event"] == "graph_stream_closed")
    result = {"space": backend.space.key, "texts": TEXTS, "records": records,
              "worker_sha256": hashlib.sha256(Path(embedding.__file__)
                  .with_name("embedding_worker.mjs").read_bytes()).hexdigest(),
              "worker_starts": sum(row["event"] == "worker_started" for row in events),
              "js_graph_bytes": observed, "baseline_vectors_equal": None,
              "scope": "Three public embedding calls; not native I/O, scale, p95 or daily admission."}
    if baseline is not None:
        previous = json.loads(external(baseline).read_text(encoding="utf-8"))
        result["baseline_vectors_equal"] = (previous["space"] == result["space"]
            and previous["texts"] == list(TEXTS) and len(previous["records"]) == len(records)
            and all(old["vectors"] == json.loads(json.dumps(new["vectors"]))
                    for old, new in zip(previous["records"], records, strict=True)))
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["worker_starts"] != 3 or observed != 0 or result["baseline_vectors_equal"] is False:
        raise ValueError("embedding_file_access_regression")
    print(json.dumps({key: value for key, value in result.items() if key not in {"records", "texts"}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    run(args.embedding_config, args.output, args.baseline)
