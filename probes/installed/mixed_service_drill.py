"""Measure overlapping host context and capture calls on an isolated synthetic scale ledger."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from importlib.metadata import version
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

from evernook import Capture, Event, LocalMiniLM, MemoryFailure, Source
from evernook.service import ENDPOINT, ServiceClient


@contextmanager
def running_service(args, *, activate):
    endpoint = args.config.parent / ENDPOINT
    previous = endpoint.read_bytes() if endpoint.exists() else None
    command = [sys.executable, "-m", "evernook.service", "--config", str(args.config),
               "--embedding-config", str(args.embedding_config), "--context-config", str(args.context_config)]
    if activate:
        command.extend(["--activate-owner", str(args.config.parent / "mcp-access-drill-admin.json")])
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 120
        while not endpoint.exists() or endpoint.read_bytes() == previous:
            if process.poll() is not None:
                raise RuntimeError(f"drill service exited before startup ({process.returncode})")
            if time.monotonic() > deadline:
                raise TimeoutError("drill service startup")
            time.sleep(.05)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def distribution(rows):
    successes = sorted(row["ms"] for row in rows if "error" not in row)
    result = {"attempts": len(rows), "successes": len(successes),
              "errors": dict(Counter(row["error"] for row in rows if "error" in row))}
    if successes:
        for p in (50, 95, 99):
            result[f"success_p{p}_ms"] = successes[math.ceil(p * len(successes) / 100) - 1]
    return result


def timed(barrier, call):
    barrier.wait(timeout=10)
    start = time.perf_counter()
    try:
        measured = call()
    except MemoryFailure as failure:
        measured = {"error": failure.code}
    end = measured.pop("response_at", time.perf_counter())
    return {**measured, "start": start, "end": end, "ms": round((end - start) * 1000, 2)}


def run(args):
    args.output.mkdir()
    config = args.config
    reader, writer = ServiceClient(config, reuse=True), ServiceClient(config, reuse=True)
    actor = reader.access_from_file(config.parent / "mcp-access-scale-writer.json")
    if actor.client_id != "synthetic-scale-writer":
        raise ValueError("requires a dedicated synthetic scale fixture")
    session = "mixed-" + uuid4().hex
    queries = {"historical": "合成条目0000001", "recent": f"合成条目{args.expected - 1:07d}",
               "no_answer": "外星深海音乐会许可证", "popular": "记录"}
    as_of = "2026-11-04T00:00:00Z"
    counter = json.loads(args.context_config.read_bytes())["tokenizer_config"]
    model = json.loads(Path(counter).read_bytes())["model_id"]

    def coverage():
        page = reader.call(actor, "recall", {"query": queries["historical"], "as_of": as_of, "timeout": 120}, timeout=135)
        return {"semantic": page["semantic"], "lexical": page["lexical"],
                "backend": page["semantic_search_backend"], "scan_limited": page["semantic_scan_limited"],
                "scanned": page["semantic_scanned_chunks"], "degraded": page["degraded"]}

    def context(case):
        query = queries[case]
        bundle = reader.context(actor, query, model_id=model, max_tokens=4096, as_of=as_of, timeout=120)
        response_at = time.perf_counter()
        payload = json.loads(bundle["payload"]) if bundle["payload"] else {}
        coverage = payload.get("coverage") or {}
        evidence = [item for item in payload.get("items", []) if item["kind"] == "evidence"]
        target = {"historical": 1, "recent": args.expected - 1}.get(case)
        target_present = None
        if target is not None:
            source = asdict(Source("synthetic-scale", "zh-scale-v1", "main", f"m{target:07d}"))
            target_present = any(item["data"]["evidence"]["evidence"]["source"] == source
                                 and query in item["data"]["excerpt"]["text"] for item in evidence)
        return {"response_at": response_at, "complete": bundle["complete"], "degraded": bundle["degraded"],
                "backend": coverage.get("semantic_search_backend"), "scan_limited": coverage.get("semantic_scan_limited"),
                "lexical_pending": (coverage.get("lexical") or {}).get("pending_messages"),
                "semantic_pending": (coverage.get("semantic") or {}).get("pending_messages"),
                "evidence_ids": [item["evidence_id"] for item in evidence], "target_present": target_present}

    receipts, reads, writes = [], [], []
    with running_service(args, activate=True) as service:
        if reader.handshake(actor)["entry"] != "synthetic-scale":
            raise ValueError("unexpected scale fixture entry")
        preflight = coverage()
        (args.output / "preflight.json").write_text(json.dumps(preflight, indent=2))
        sem = preflight["semantic"]
        if (sem["state"] != "ready" or sem["space_id"] != LocalMiniLM.space.key
                or sem["source_messages"] != args.expected or sem["indexed_messages"] != args.expected
                or sem["stored_vectors"] != args.expected or sem["failed_chunks"] != 0
                or preflight["backend"] not in {"python-exact", "sqlite-vec-0.1.9-exact"}
                or preflight["scan_limited"] or preflight["scanned"] != args.expected):
            raise ValueError("preflight did not find complete real MiniLM coverage")
        warmup = {case: context(case) for case in queries}
        cases = tuple(queries)
        with ThreadPoolExecutor(max_workers=2) as pool, (args.output / "samples.jsonl").open("x") as samples:
            for i in range(args.rounds):
                event = Event(Source("synthetic-scale", session, "main", str(i)), "user", i, None,
                              "complete", f"合成并发写入样本 {i}。")
                capture = Capture(event, "message")
                barrier = threading.Barrier(2)

                def submit():
                    receipt = writer.submit_capture(actor, capture)
                    response_at = time.perf_counter()
                    if not receipt.message.original_saved:
                        return {"response_at": response_at, "error": "original_not_saved"}
                    receipts.append((capture, receipt))
                    return {"response_at": response_at, "message_id": receipt.message.message_id}

                case = cases[i % len(cases)]
                read = pool.submit(timed, barrier, lambda: context(case))
                write = pool.submit(timed, barrier, submit)
                reads.append(read.result())
                reads[-1]["case"] = case
                writes.append(write.result())
                print(json.dumps({"i": i, "read": reads[-1], "write": writes[-1]}), file=samples, flush=True)
        # This extra call is outside the timed distribution. Kill only the service
        # we started, keeping the caller and its independently held receipts alive.
        started = threading.Event()

        def interrupted_read():
            started.set()
            try:
                context("historical")
            except MemoryFailure as failure:
                return failure.code
            return "returned"

        with ThreadPoolExecutor(1) as crash_pool:
            interrupted = crash_pool.submit(interrupted_read)
            if not started.wait(timeout=10):
                raise TimeoutError("fault probe caller did not start")
            time.sleep(.1)
            pending_at_kill = not interrupted.done()
            recovering = time.perf_counter()
            service.kill()
            fault_result = interrupted.result(timeout=140)
    verified = 0
    first_ms = None
    with running_service(args, activate=False):
        for capture, receipt in receipts:
            message = reader.read(actor, receipt.message.message_id)
            if message is None or message.event != capture.event or message.receipt != receipt.message:
                raise ValueError("acknowledged original differed after restart")
            if first_ms is None:
                first_ms = round((time.perf_counter() - recovering) * 1000, 2)
            if writer.submit_capture(actor, capture) != receipt:
                raise ValueError("same-source retry changed the receipt")
            verified += 1
        after = coverage()
    reader.close()
    writer.close()
    overlaps = sum(max(r["start"], w["start"]) < min(r["end"], w["end"])
                   for r, w in zip(reads, writes, strict=True))
    by_case = {case: [r for r in reads if r["case"] == case] for case in queries}
    report = {"installed_version": version("evernook"), "queries": queries, "as_of": as_of, "host_model": model,
              "tokens": 4096, "embedding_space": asdict(LocalMiniLM.space), "warmup": warmup, "preflight": preflight,
              "rounds": args.rounds, "overlapping_pairs": overlaps, "context": distribution(reads),
              "capture": distribution(writes), "contexts_by_case": {case: distribution(rows) for case, rows in by_case.items()},
              "expected_source_and_text_found": {case: sum(bool(r.get("target_present")) for r in by_case[case])
                                                 for case in ("historical", "recent")},
              "no_answer_with_evidence": sum(bool(r.get("evidence_ids")) for r in by_case["no_answer"]),
              "complete_contexts": sum(bool(r.get("complete")) for r in reads),
              "degraded_contexts_by_reason": dict(Counter(code for r in reads for code in r.get("degraded", []))),
              "client_read_pending_at_kill": pending_at_kill, "interrupted_read_result": fault_result,
              "kill_to_first_verified_read_ms": first_ms, "acknowledged_and_replayed": verified,
              "after_restart": after}
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("context", "capture", "overlapping_pairs",
                                             "kill_to_first_verified_read_ms", "acknowledged_and_replayed")}))
    post = after["semantic"]
    return (verified == overlaps == args.rounds == report["context"]["successes"]
            and pending_at_kill and fault_result == "service_unavailable"
            and after["backend"] == preflight["backend"] and after["scanned"] == args.expected and not after["scan_limited"]
            and post["space_id"] == sem["space_id"] and post["source_messages"] == args.expected + verified
            and post["indexed_messages"] == args.expected and post["failed_chunks"] == 0
            and post["stored_vectors"] == args.expected and post["pending_messages"] == verified)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "context-config", "embedding-config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected", required=True, type=int, help="Expected complete single-chunk source count")
    parser.add_argument("--rounds", type=int, default=32, help="Paired read/write rounds, cycling through four query types")
    args = parser.parse_args()
    if args.expected <= 0 or not 1 <= args.rounds <= 500:
        parser.error("expected a positive fixture size and 1..500 rounds")
    raise SystemExit(0 if run(args) else 2)
