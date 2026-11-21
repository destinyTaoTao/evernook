"""Compare two Core source trees through public recall on the same synthetic snapshot."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack, closing
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from statistics import median
import sqlite3
import subprocess
import sys
import time
from typing import Any

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall, semantic
from evernook.models import MemoryFailure
from evernook.paths import external
from probes.admission.candidate_index_scale import index_objects
from probes.admission.synthetic_full_vector_scale import POLICY, SyntheticFingerprints, copy_runtime


ARMS = ("baseline", "candidate")
SCAN_COUNT = "semantic_scanned_chunks"
DISTANCE_BACKEND = "sqlite-vec-0.1.9-exact"
MODULE = "probes.admission.recall_shared_scan"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def candidates(size: int) -> dict[str, tuple[str, str | None]]:
    return {
        "recent": (f"合成条目{size - 1:07d}", f"m{size - 1:07d}"),
        "historical": ("合成条目0000001", "m0000001"),
        "no_answer": ("外星深海音乐会许可证", None),
    }


def worker(args: argparse.Namespace) -> None:
    runtime = MemoryRuntime(args.source / "runtime.json")
    access = runtime.access_from_file(args.source / "mcp-access-scale-writer.json")
    backend = LocalMiniLM.from_file(args.embedding_config) if args.embedding_config else SyntheticFingerprints()
    recall = SemanticRecall(runtime, backend)
    package = Path(semantic.__file__).resolve().parent
    startup = {
        "source": str(package),
        "files": {p.relative_to(package).as_posix(): sha256(p.read_bytes())
                  for p in sorted(package.rglob("*")) if p.suffix in {".py", ".mjs"}},
        "index_enabled": recall.candidate_index_enabled(access),
        "semantic": asdict(recall.status(access)),
        "embedding_space": asdict(backend.space),
    }
    print(canonical(startup).decode("ascii"), flush=True)
    as_of = json.loads(POLICY.read_bytes())["scale"]["as_of"]
    for command in sys.stdin:
        query = json.loads(command)
        started = time.perf_counter()
        try:
            page = recall.recall(access, query, limit=10, as_of=as_of, timeout=args.timeout)
        except MemoryFailure as failure:
            result = {"seconds": time.perf_counter() - started, "error_code": failure.code}
        else:
            result = {"seconds": time.perf_counter() - started, "page": asdict(page)}
        print(canonical(result).decode("ascii"), flush=True)


def stop(worker_process: subprocess.Popen[str]) -> None:
    assert worker_process.stdin is not None
    worker_process.stdin.close()
    try:
        worker_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker_process.kill()
        worker_process.wait()


def new_worker(stack: ExitStack, args: argparse.Namespace, arm: str, src: Path, target: Path,
               output: Path) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    cmd = [sys.executable, "-m", MODULE, "--worker", "--source", str(target), "--timeout", str(args.timeout)]
    if args.embedding_config:
        cmd.extend(("--embedding-config", str(args.embedding_config)))
    stderr_path = output / f"{arm}.stderr.log"
    stderr = stack.enter_context(stderr_path.open("x", encoding="utf-8"))
    process = stack.enter_context(subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
        text=True, encoding="ascii", env={**os.environ, "PYTHONPATH": str(src)}))
    stack.callback(stop, process)
    assert process.stdout is not None
    startup_line = process.stdout.readline()
    if not startup_line:
        raise RuntimeError(f"{arm} worker exited during startup, see {stderr_path}")
    startup: dict[str, Any] = json.loads(startup_line)
    if Path(startup["source"]) != src / "evernook" or not startup["index_enabled"]:
        raise ValueError(f"{arm} imported the wrong Core or candidate indexes are not enabled")
    status = startup["semantic"]
    if (status["state"], status["source_messages"], status["stored_vectors"], status["indexed_messages"],
            status["pending_messages"], status["failed_chunks"]) != ("ready", args.size, args.size, args.size, 0, 0):
        raise ValueError(f"{arm} fixture does not have the requested full semantic coverage")
    return process, startup


def validate_fixture(target: Path, size: int) -> None:
    with closing(sqlite3.connect((target / "memory.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        total, synthetic = db.execute("""SELECT count(*), count(*) FILTER (WHERE
            json_extract(event,'$.source.entry')='synthetic-scale' AND
            json_extract(event,'$.source.session')='zh-scale-v1' AND
            json_extract(event,'$.source.branch')='main') FROM messages""").fetchone()
    if total != size or synthetic != size:
        raise ValueError("Expected only original messages from the synthetic scale fixture")


def measure(process: subprocess.Popen[str], query: str, expected: str | None, size: int, result_path: Path) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(canonical(query).decode("ascii") + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"Worker exited without a result; see logs in {result_path.parent}")
    result = json.loads(line)
    result_path.write_bytes(canonical(result) + b"\n")
    row: dict[str, Any] = {"seconds": result["seconds"], "error_code": result.get("error_code"),
                          "artifact": result_path.name, "valid_page": False}
    if "page" not in result:
        return row
    page = result["page"]
    hit_events = [hit["evidence"]["source"]["event"] for hit in page["hits"]]
    evidence = ((not hit_events and page["evidence_state"] == "no_matching_evidence") if expected is None
                else (len(hit_events) == 10 and hit_events[0] == expected and page["evidence_state"] == "candidates"))
    row.update(page_sha256=sha256(canonical(page)), semantic=page["semantic"], lexical=page["lexical"],
               evidence_state=page["evidence_state"], degraded=page["degraded"], ordered_source_events=hit_events,
               semantic_scan_limited=page["semantic_scan_limited"], recent_window_limited=page["recent_window_limited"],
               semantic_search_backend=page["semantic_search_backend"])
    row[SCAN_COUNT] = page[SCAN_COUNT]
    row["normalized_page_sha256"] = sha256(canonical({key: value for key, value in page.items() if key != SCAN_COUNT}))
    row["valid_page"] = (evidence and not page["degraded"] and not page["semantic_scan_limited"]
        and not page["recent_window_limited"] and page["semantic_search_backend"] == DISTANCE_BACKEND
        and page["semantic"]["state"] == "ready" and page["semantic"]["stored_vectors"] == size
        and page["lexical"]["state"] == "ready" and page["lexical"]["indexed_messages"] == size)
    return row


def summary(pairs: dict[str, list[dict[str, Any]]], rounds: int) -> dict[str, Any]:
    output = {}
    for query, rows in pairs.items():
        equal = sum(row["same_page"] for row in rows)
        good = sum(row["passed"] for row in rows)
        cell: dict[str, Any] = {"pairs": len(rows), "normalized_equal_pairs": equal, "valid_equal_pairs": good,
                               "expected_pairs": rounds, "p95_rank": math.ceil(.95 * len(rows))}
        if good == len(rows):
            for arm in ARMS:
                values = sorted(row[arm]["seconds"] for row in rows)
                cell[arm] = {"median_seconds": median(values), "p95_seconds": values[cell["p95_rank"] - 1],
                             "scanned_chunks": sorted({row[arm][SCAN_COUNT] for row in rows})}
        else:
            cell["failed_pairs"] = [row for row in rows if not row["passed"]]
        output[query] = cell
    return output


def compare(args: argparse.Namespace) -> bool:
    output = external(args.output)
    output.mkdir()
    target = output / "runtime"
    copy_runtime(external(args.source), target)
    validate_fixture(target, args.size)
    source_paths = {"baseline": args.baseline_src.resolve(strict=True), "candidate": args.candidate_src.resolve(strict=True)}
    queries = candidates(args.size)
    pairs: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    with ExitStack() as stack:
        runs = {arm: new_worker(stack, args, arm, src, target, output) for arm, src in source_paths.items()}
        old, new = (runs[arm][1] for arm in ARMS)
        if old["semantic"] != new["semantic"] or old["embedding_space"] != new["embedding_space"]:
            raise ValueError("The two arms did not open the same embedding space and snapshot")
        changed = sorted(path for path in old["files"].keys() | new["files"].keys()
                         if old["files"].get(path) != new["files"].get(path))
        if not changed:
            raise ValueError("The two arms loaded identical Core code")
        manifest = {"size": args.size, "rounds": args.rounds, "python": platform.python_version(),
            "python_executable": sys.executable, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
            "queries": queries, "as_of": json.loads(POLICY.read_bytes())["scale"]["as_of"], "limit": 10,
            "timeout_seconds": args.timeout, "source_snapshot": str(args.source), "arms": dict(zip(ARMS, (old, new))),
            "changed_core_files": changed, "p95_method": "nearest rank ceil(0.95*n)",
            "probe_sha256": sha256(Path(__file__).read_bytes()),
            "policy_sha256": sha256(POLICY.read_bytes()), "ignored_dto_fields": [SCAN_COUNT],
            "index_definitions": index_objects(target / "memory.sqlite3"),
            "embedding_config_sha256": sha256(args.embedding_config.read_bytes()) if args.embedding_config else None}
        (output / "manifest.json").write_bytes(canonical(manifest) + b"\n")
        names = list(queries)
        with (output / "timings.jsonl").open("x", encoding="ascii") as log:
            for index in range(args.rounds):
                order = ARMS if index % 2 == 0 else ARMS[::-1]
                offset = index % len(names)
                for name in names[offset:] + names[:offset]:
                    query, expected = queries[name]
                    row: dict[str, Any] = {"round": index + 1, "query": name, "order": order}
                    for arm in order:
                        path = output / f"round-{index + 1:02d}-{name}-{arm}.json"
                        row[arm] = measure(runs[arm][0], query, expected, args.size, path)
                    old_hash = row["baseline"].get("normalized_page_sha256")
                    row["same_page"] = old_hash is not None and old_hash == row["candidate"].get("normalized_page_sha256")
                    row["passed"] = row["same_page"] and all(row[arm]["valid_page"] for arm in ARMS)
                    pairs[name].append(row)
                    log.write(canonical(row).decode("ascii") + "\n")
                    log.flush()
                    print(index + 1, name, *(f"{arm}={row[arm]['seconds']:.3f}s" for arm in order),
                          f"valid_equal={row['passed']}", flush=True)
                (output / "checkpoint.json").write_bytes(canonical(summary(pairs, args.rounds)) + b"\n")
    return all(row["passed"] for rows in pairs.values() for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, required=True, help="Existing indexed synthetic runtime")
    parser.add_argument("--embedding-config", type=Path, help="Use local MiniLM; omit for synthetic fingerprint vectors")
    parser.add_argument("--baseline-src", type=Path, help="Baseline src directory containing evernook")
    parser.add_argument("--candidate-src", type=Path, help="Candidate src directory containing evernook")
    parser.add_argument("--output", type=Path, help="New external evidence directory")
    parser.add_argument("--size", type=int, choices=(100000, 1000000))
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.worker:
        worker(args)
        return
    if (any(value is None for value in (args.baseline_src, args.candidate_src, args.output, args.size))
            or args.rounds < 2 or args.rounds % 2):
        parser.error("Specify both source trees, output, size and an even number of rounds of at least two")
    if args.embedding_config:
        args.embedding_config = args.embedding_config.resolve(strict=True)
    raise SystemExit(0 if compare(args) else 2)


if __name__ == "__main__":
    main()
