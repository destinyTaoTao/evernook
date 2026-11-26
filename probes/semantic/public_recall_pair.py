"""Alternate public recall on two frozen revisions and private synthetic fixture copies."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile


REVISIONS = {"baseline": "6b21cf1990db53b4d7f38e3ce0f40b4247b883b6",
             "candidate": "a95f611cec18140fd55cd85912d32311f6e2e7a3"}
QUERIES = {"recent": "合成条目0999999", "history": "合成条目0000001", "absent": "鲸鲨泥鳅"}
AS_OF = "2026-11-04T00:00:00Z"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def packed(data: object) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False)


def worker(snapshot: Path, fixture: Path, arm: str) -> None:
    sys.path[:0] = [str(snapshot / "src"), str(snapshot)]
    import evernook
    from evernook import MemoryFailure, MemoryRuntime, SemanticRecall
    from evernook.paths import provision_private_directory
    from probes.admission.synthetic_full_vector_scale import SyntheticFingerprints

    if Path(evernook.__file__).resolve().parent != (snapshot / "src/evernook").resolve():
        raise RuntimeError("unexpected_source")
    root = snapshot.parent / (arm + "-ledger")
    provision_private_directory(root)
    for name in ("client.json", "idempotency.key", "mcp-access-scale-maintenance.json", "mcp-access-scale-writer.json", "memory.sqlite3"):
        shutil.copyfile(fixture / name, root / name)
    db_hash = sha(root / "memory.sqlite3")
    config = root / "runtime.json"
    config.write_text(packed({**json.loads((fixture / config.name).read_bytes()), "data_dir": str(root.resolve())}), encoding="utf-8")
    start = time.perf_counter()
    runtime = MemoryRuntime(config)
    opened = time.perf_counter() - start
    start = time.perf_counter()
    reader = runtime.access_from_file(root / "mcp-access-scale-writer.json")
    credential = time.perf_counter() - start
    maintain = runtime.access_from_file(root / "mcp-access-scale-maintenance.json")
    workflow = SemanticRecall(runtime, SyntheticFingerprints())
    if not runtime.lexical_checkpoint_enabled(maintain) or not workflow.candidate_index_enabled(maintain):
        raise RuntimeError("synthetic_fixture_indexes_required")
    status = workflow.status(reader)
    if (status.source_messages, status.indexed_messages, status.stored_vectors) != (1_000_000,) * 3:
        raise RuntimeError("wrong_synthetic_fixture")
    build = None
    if arm == "candidate":
        start = time.perf_counter()
        workflow.configure_coverage_checkpoint(maintain, enabled=True)
        build = time.perf_counter() - start
        if workflow.status(reader) != status or not runtime.lexical_checkpoint_enabled(maintain):
            raise RuntimeError("checkpoint_changed_coverage")
    print(packed({"sha256": db_hash, "open": opened, "reader_credentials": credential,
                  "build": build, "coverage": asdict(status), "sqlite": sqlite3.sqlite_version}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        query = QUERIES[request["query"]]
        cancel = threading.Event()
        timer = None
        fired: list[float] = []
        child_seconds: list[float] = []
        seconds: defaultdict[str, float] = defaultdict(float)
        calls: Counter[str] = Counter()

        def instrument(obj: object, name: str, label: str) -> Any:
            original = getattr(obj, name)
            def timed(*a: Any, **kw: Any) -> Any:
                nonlocal timer
                if label == "lexical_coverage" and request["cancel"]:
                    def interrupt() -> None:
                        fired.append(time.perf_counter())
                        cancel.set()
                    timer = threading.Timer(.01, interrupt)
                    timer.start()
                t = time.perf_counter()
                child_seconds.append(0.)
                try:
                    return original(*a, **kw)
                finally:
                    elapsed = time.perf_counter() - t
                    seconds[label] += elapsed - child_seconds.pop()
                    calls[label] += 1
                    if child_seconds:
                        child_seconds[-1] += elapsed
            return patch.object(obj, name, timed)

        with ExitStack() as hooks:
            for obj, name, label in (
                (runtime, "_audit", "audit"), (runtime, "_search_coverage", "lexical_coverage"),
                (runtime, "_search_snapshot", "lexical_search"), (workflow, "_recent", "recent_scope"),
                (workflow, "_keyword", "keyword"), (workflow, "_status", "semantic_status"),
                (workflow, "_semantic", "vectors"),
            ):
                hooks.enter_context(instrument(obj, name, label))
            start = time.perf_counter()
            try:
                page = workflow.recall(reader, query, limit=10, as_of=AS_OF, timeout=120, cancel=cancel)
                page_data, error = asdict(page), None
            except MemoryFailure as failure:
                page_data, error = None, failure.code
            end = time.perf_counter()
        if timer is not None:
            timer.join()
        seconds["other"] = end - start - sum(seconds.values())
        print(packed({"seconds": end - start, "phases": seconds, "calls": calls, "page": page_data,
                      "error": error, "cancel_to_return": end-fired[0] if fired else None}), flush=True)


def run(fixture: Path, output: Path, rounds: int) -> None:
    repo = Path(__file__).resolve().parents[2]
    fixture = fixture.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(repo) or output.is_relative_to(fixture) or rounds < 2:
        raise ValueError("new_external_output_and_two_rounds_required")
    output.mkdir()
    fixture_db = fixture / "memory.sqlite3"
    wal = fixture / "memory.sqlite3-wal"
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("offline_fixture_required")
    def log(**data: object) -> None:
        with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(packed({"at": datetime.now(timezone.utc).isoformat(), **data}) + "\n")
        print(packed(data), flush=True)
    log(stage="source_hash")
    original = sha(fixture_db)
    processes: dict[str, subprocess.Popen[str]] = {}
    rows: list[dict[str, Any]] = []
    pages: dict[str, Any] = {}
    with ExitStack() as cleanup:
        for arm, revision in REVISIONS.items():
            log(stage="prepare", arm=arm, revision=revision)
            snapshot = output / arm
            archive = output / (arm + ".zip")
            subprocess.run(["git", "-C", str(repo), "archive", "--format=zip", "--output=" + str(archive), revision,
                            "src/evernook", "probes/admission/synthetic_full_vector_scale.py"], check=True)
            with ZipFile(archive) as z:
                z.extractall(snapshot)
            # -I ignores PYTHONPATH and prevents the working source from winning over the pinned snapshot.
            process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "worker",
                                        str(snapshot), str(fixture), arm], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=cleanup.enter_context((output / (arm + "-stderr.log")).open("w", encoding="utf-8")),
                                       cwd=output, text=True, encoding="utf-8")
            cleanup.callback(process.wait)
            cleanup.callback(process.terminate)
            assert process.stdout and process.stdin
            cleanup.callback(process.stdout.close)
            cleanup.callback(process.stdin.close)
            bootstrap = json.loads(process.stdout.readline())
            if bootstrap["sha256"] != original:
                raise RuntimeError("fixture_copy_mismatch")
            processes[arm] = process
            log(stage="ready", arm=arm, **bootstrap)

        def sample(arm: str, case: str, iteration: int, *, cancel: bool = False) -> None:
            process = processes[arm]
            assert process.stdin and process.stdout
            log(stage="start", arm=arm, case=case, iteration=iteration, cancel=cancel)
            process.stdin.write(packed({"query": case, "cancel": cancel}) + "\n")
            process.stdin.flush()
            sample = json.loads(process.stdout.readline())
            page = sample.pop("page")
            if cancel:
                if sample["error"] != "recall_canceled" or sample["cancel_to_return"] is None:
                    raise RuntimeError("cancel_not_acknowledged")
            else:
                if sample["error"] or page is None or page["degraded"] or page["semantic_scanned_chunks"] != 1_000_000:
                    raise RuntimeError(("incomplete_page", arm, case, sample["error"], page))
                if (case == "absent") != (not page["hits"] and page["evidence_state"] == "no_matching_evidence"):
                    raise RuntimeError(("unexpected_evidence", arm, case))
                if case != "absent" and len(page["hits"]) != 10:
                    raise RuntimeError(("missing_hits", arm, case))
                page_path = output / f"{arm}-{case}-{iteration}.json"
                page_path.write_text(packed(page), encoding="utf-8")
                if page != pages.setdefault(case, page):
                    raise RuntimeError(("full_dto_mismatch", arm, case, page_path))
                sample["sha256"] = sha(page_path)
            row = {"arm": arm, "case": case, "round": iteration, "cancel": cancel, **sample}
            rows.append(row)
            log(stage="done", **row)
            (output / "samples.json").write_text(packed(rows), encoding="utf-8")

        for arm in processes:
            sample(arm, "recent", -1, cancel=True)
            sample(arm, "recent", 0)
        for iteration in range(1, rounds + 1):
            for offset, case in enumerate(QUERIES):
                for arm in (("baseline", "candidate") if (iteration+offset) % 2 else ("candidate", "baseline")):
                    sample(arm, case, iteration)
    if sha(fixture_db) != original or wal.exists() and wal.stat().st_size:
        raise RuntimeError("source_fixture_changed")
    log(stage="complete", rounds=rounds, source_sha256=original)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("fixture", type=Path)
        parser.add_argument("output", type=Path)
        parser.add_argument("--rounds", type=int, default=2)
        args = parser.parse_args()
        run(args.fixture, args.output, args.rounds)
