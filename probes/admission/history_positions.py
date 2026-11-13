"""Prepare/query the frozen >10000-source position fixture with real pinned embeddings."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from evernook import Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize, pair_client
from evernook.learning_evaluation import load_suite
from evernook.paths import external
from learning import implementation_identity, save


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "query"))
    parser.add_argument("--suite", type=Path, default=Path("eval/zh-learning-v1"))
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=900)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("--seconds must be 1..1800")
    suite = load_suite(args.suite)
    backend = LocalMiniLM.from_file(args.embedding_config)
    steps = [step for step in suite.steps if step["scenario"] == "position"]
    root = external(args.output)
    root.mkdir()
    started = time.monotonic()
    identity = implementation_identity()
    if args.phase == "prepare":
        config, access = initialize(root / "runtime", client_id="synthetic-history", entry="fixture")
        pair_client(config, client_id="synthetic-history", entry="fixture", access_path=config.parent / "mcp-access-history.json")
        runtime = MemoryRuntime(config)
        # Use the exact public paired token persisted above for preparation and future queries.
        access = runtime.access_from_file(config.parent / "mcp-access-history.json")
        ids: dict[str, str] = {}
        save(root / "manifest.json", {"suite_hash": suite.suite_hash, "implementation": identity,
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "space": asdict(backend.space),
            "seconds": args.seconds, "scope": "Real frozen 10004-source Core preparation; not 100k/1m or host/model admission."})
        with (root / "receipts.jsonl").open("w", encoding="utf-8") as receipts:
            for index, alias in enumerate(steps[0]["add"]):
                if time.monotonic() - started >= args.seconds:
                    save(root / "preparation.json", {"error_code": "capture_budget", "captured": len(ids), "ids": ids})
                    return 2
                source = suite.sources[alias]
                receipt = runtime.submit(access, Event(Source("fixture", source["session"], "main", alias),
                    source["role"], index, source["at"], "complete", source["text"]))
                ids[alias] = receipt.message_id
                receipts.write(json.dumps({"alias": alias, "receipt": asdict(receipt)}) + "\n")
                if index % 128 == 0:
                    receipts.flush()
                    print(json.dumps({"captured": len(ids)}), flush=True)
        save(root / "aliases.json", ids)
        recall = SemanticRecall(runtime, backend)
        status = recall.status(access)
        with (root / "progress.jsonl").open("w", encoding="utf-8") as progress:
            while status.pending_messages:
                remaining = args.seconds - (time.monotonic() - started)
                if remaining <= 0:
                    break
                status = recall.refresh(access, limit=16, timeout=min(30, remaining))
                progress.write(json.dumps({"elapsed_seconds": time.monotonic() - started, "semantic": asdict(status)}) + "\n")
                progress.flush()
                if (status.stored_vectors or 0) % 256 == 0:
                    print(json.dumps({"vectors": status.stored_vectors, "pending": status.pending_messages}), flush=True)
        save(root / "preparation.json", {"semantic": asdict(status), "captured": len(ids),
            "elapsed_seconds": time.monotonic() - started, "complete": status.state == "ready", "admission": "pending"})
        return 0 if status.state == "ready" else 2
    if args.fixture is None:
        parser.error("query requires --fixture")
    fixture = external(args.fixture)
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    if manifest["suite_hash"] != suite.suite_hash or manifest["space"] != asdict(backend.space):
        raise ValueError("history_fixture_identity_mismatch")
    runtime = MemoryRuntime(fixture / "runtime" / "runtime.json")
    access = runtime.access_from_file(runtime.root / "mcp-access-history.json")
    ids = json.loads((fixture / "aliases.json").read_text(encoding="utf-8"))
    aliases = {value: key for key, value in ids.items()}
    recall = SemanticRecall(runtime, backend)
    results = []
    for step in steps:
        before = time.perf_counter()
        page = recall.recall(access, step["question"], as_of=step["as_of"], timeout=30)
        results.append({"case_id": step["id"], "source_ids": [aliases[hit.evidence.message_id] for hit in page.hits],
            "elapsed_ms": (time.perf_counter() - before) * 1000, "page": asdict(page)})
        save(root / "results.json", {"suite_hash": suite.suite_hash, "implementation": identity,
            "producer": manifest, "results": results, "admission": "pending"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
