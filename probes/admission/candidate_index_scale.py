"""Time public recall on an isolated synthetic scale fixture and retain the complete result pages."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall
from evernook import semantic
from evernook.models import MemoryFailure
from evernook.paths import external


CANDIDATE_INDEX_NAMES = ("evernook_recall_source_v1", "evernook_recall_time_v1")


def index_objects(db_path: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name COLLATE NOCASE IN (?,?) ORDER BY name",
            CANDIDATE_INDEX_NAMES)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True, help="Isolated copy of a synthetic scale runtime")
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, choices=(100000, 1000000), required=True)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--enable-index", action="store_true",
                        help="Build the candidate index on this isolated copy before measuring")
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    runtime_dir = external(args.runtime)
    config_path = runtime_dir / "runtime.json"
    if external(Path(json.loads(config_path.read_text(encoding="utf-8"))["data_dir"])) != runtime_dir:
        raise ValueError("runtime config must point at the supplied fixture directory")
    output = external(args.output)
    output.mkdir()
    db_path = runtime_dir / "memory.sqlite3"
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    policy_path = Path(__file__).with_name("policy.json")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    semantic_file = Path(semantic.__file__)
    source_files = [semantic_file, semantic_file.with_name("recall_index.py"), semantic_file.with_name("runtime.py")]
    manifest: dict[str, object] = {"runtime": str(runtime_dir), "source_messages": messages,
        "enable_index": args.enable_index, "samples_per_query": args.samples, "as_of": policy["scale"]["as_of"],
        "before_objects": index_objects(db_path), "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files if p.exists()}}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if manifest["before_objects"]:
        raise ValueError("candidate index names must both be absent before either arm")
    if messages != args.size:
        raise ValueError(f"expected {args.size} messages, found {messages}")
    runtime = MemoryRuntime(config_path)
    reader = runtime.access_from_file(runtime_dir / "mcp-access-scale-writer.json")
    backend = LocalMiniLM.from_file(args.embedding_config)
    recall = SemanticRecall(runtime, backend)
    if args.enable_index:
        maintainer = runtime.access_from_file(runtime_dir / "mcp-access-scale-maintenance.json")
        before_bytes = db_path.stat().st_size
        started = time.perf_counter()
        recall.configure_candidate_index(maintainer, enabled=True)
        build = {"seconds": time.perf_counter() - started, "db_bytes_before": before_bytes,
                 "db_bytes_after": db_path.stat().st_size}
        (output / "build.json").write_text(json.dumps(build), encoding="utf-8")
    objects = index_objects(db_path)
    # Older baseline revisions have no public candidate-index status method.
    status = getattr(recall, "candidate_index_enabled", None)
    enabled = status(reader) if status is not None else False
    manifest.update(after_objects=objects, effective_index_enabled=enabled, public_status_available=status is not None,
                    embedding_space=backend.space.key)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    expected = set(CANDIDATE_INDEX_NAMES) if args.enable_index else set()
    if (enabled is not args.enable_index or len(objects) != len(expected) or {obj["name"] for obj in objects} != expected
            or any(obj["type"] != "index" or obj["tbl_name"] != "messages" for obj in objects)):
        raise ValueError("effective candidate index state does not match the requested arm")
    queries = {"recent": f"合成条目{args.size - 1:07d}", "historical": "合成条目0000001",
               "no_answer": "外星深海音乐会许可证"}
    for sample in range(1, args.samples + 1):
        for name, query in queries.items():
            started = time.perf_counter()
            try:
                page = recall.recall(reader, query, limit=10, as_of=policy["scale"]["as_of"], timeout=120)
                elapsed = time.perf_counter() - started
                result: dict[str, object] = {"page": asdict(page)}
            except MemoryFailure as failure:
                elapsed = time.perf_counter() - started
                result = {"error_code": failure.code}
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            (output / f"{sample}-{name}.json").write_bytes(encoded)
            row = {"sample": sample, "query": name, "query_text": query, "size": args.size,
                   "seconds": elapsed, "result_sha256": hashlib.sha256(encoded).hexdigest(),
                   "error": result.get("error_code")}
            with (output / "timings.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
