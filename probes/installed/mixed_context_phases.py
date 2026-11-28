"""Measure four instrumented context/capture pairs on a dedicated installed scale ledger."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import inspect
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import CodeType
from uuid import uuid4

from evernook import Capture, Event, LocalMiniLM, Source
from evernook import lexical_checkpoint, semantic_checkpoint, service
from evernook.context import ContextBuilder
from evernook.host_context import HostContext
from evernook.runtime import MemoryRuntime
from evernook.semantic import SemanticRecall
from evernook.tokenizer import _TokenSession


def install_trace(target: Path):
    # Local monitoring events only at selected code objects. Unlike cProfile this avoids
    # intercepting every SQLite progress callback during million-row queries.
    nested = next(code for code in ContextBuilder._build.__code__.co_consts
                  if isinstance(code, CodeType) and code.co_name == "usable_now")
    stages = {fn.__code__: label for fn, label in (
        (service.dispatch, "dispatch"), (HostContext.build, "host_context"),
        (ContextBuilder.build, "context_build"), (HostContext._configured, "host_config"),
        (SemanticRecall.recall, "recall"), (MemoryRuntime._search_coverage, "lexical_coverage"),
        (lexical_checkpoint.search_state, "lexical_checkpoint"), (MemoryRuntime._search_index, "lexical_fallback"),
        (MemoryRuntime._search_snapshot, "literal_search"), (MemoryRuntime._search_hits, "literal_rows"),
        (SemanticRecall._recent, "recent_window"), (SemanticRecall._keyword, "keyword_hits"),
        (SemanticRecall._keyword_ids, "bm25_ids"), (SemanticRecall.status, "semantic_coverage"),
        (semantic_checkpoint.status, "semantic_checkpoint"), (LocalMiniLM.embed, "embedding"),
        (SemanticRecall._semantic, "semantic_candidates"), (MemoryRuntime.read, "source_read"),
        (ContextBuilder._tokens, "context_tokens"), (MemoryRuntime.handshake, "handshake"),
    )}
    stages[nested] = "context_revalidate"
    intervals = {}
    for fn, start_text, end_text, label in (
        (SemanticRecall._semantic, 'rows = db.execute(f"""WITH ranked', 'scanned = int(', 'vector_sql'),
        (SemanticRecall._recall, 'if selected:', 'if source_changed:', 'recall_revalidate'),
    ):
        lines, first = inspect.getsourcelines(fn)
        def unique_line(prefix):
            match, = [first + i for i, line in enumerate(lines) if line.lstrip().startswith(prefix)]
            return match
        intervals[fn.__code__] = (unique_line(start_text), unique_line(end_text), label)

    local = threading.local()
    unwound = object()
    def push(label):
        local.stack.append([label, time.perf_counter_ns(), 0])

    def pop(label):
        name, start, children = local.stack.pop()
        if name != label:
            raise RuntimeError("unbalanced phase timing")
        elapsed = time.perf_counter_ns() - start
        if local.stack:
            local.stack[-1][2] += elapsed
        row = local.times[label]
        row[0] += 1
        row[1] += elapsed
        row[2] += elapsed - children
        return elapsed / 1e6

    def entering(code, offset):
        if getattr(local, "active", False):
            push(stages[code])

    def leaving(code, offset, result):
        if not getattr(local, "active", False):
            return
        label = stages.get(code)
        if code in intervals and local.stack and local.stack[-1][0] == intervals[code][2]:
            pop(intervals[code][2])
        if label:
            elapsed_ms = pop(label)
            if result is unwound:
                local.coverage.setdefault("unwound_stages", []).append(label)
                return
            if label == "literal_search" and hasattr(result, "hits"):
                local.coverage.setdefault("literal_calls", []).append({
                    "elapsed_ms": elapsed_ms, "hits": len(result.hits), "reason": result.fallback_reason})
            if label in ("lexical_checkpoint", "semantic_checkpoint"):
                local.coverage[label + "_used"] = result is not None
            if label == "recall" and hasattr(result, "semantic_scanned_chunks"):
                local.coverage.update(backend=result.semantic_search_backend, scanned=result.semantic_scanned_chunks,
                                      scan_limited=result.semantic_scan_limited, lexical=asdict(result.lexical),
                                      semantic=asdict(result.semantic))

    def line_event(code, line):
        if not getattr(local, "active", False):
            return
        first, end, label = intervals[code]
        if line == first and local.stack[-1][0] != label:
            push(label)
        elif line == end and local.stack[-1][0] == label:
            pop(label)

    def unwinding(code, offset, exception):
        leaving(code, offset, unwound)

    monitor = sys.monitoring
    ident = monitor.PROFILER_ID
    monitor.use_tool_id(ident, "mixed-context-phases")
    events = monitor.events
    for event, callback in ((events.PY_START, entering), (events.PY_RETURN, leaving),
                            (events.PY_UNWIND, unwinding), (events.LINE, line_event)):
        monitor.register_callback(ident, event, callback)
    monitor.set_events(ident, events.PY_UNWIND)
    for code in stages.keys() | intervals.keys():
        mask = events.PY_RETURN
        if code in stages:
            mask |= events.PY_START
        if code in intervals:
            mask |= events.LINE
        monitor.set_local_events(ident, code, mask)

    dispatch = service.dispatch
    def measured(runtime, request, **options):
        if request.get("operation") != "context":
            return dispatch(runtime, request, **options)
        local.stack, local.times, local.coverage = [], defaultdict(lambda: [0, 0, 0]), {}
        local.active = True
        try:
            return dispatch(runtime, request, **options)
        finally:
            local.active = False
            if local.stack:
                raise RuntimeError("phase timing left open spans")
            row = {"query": request["arguments"]["query"], **local.coverage,
                   "stages": {label: {"calls": calls, "inclusive_ms": elapsed / 1e6, "exclusive_ms": own / 1e6}
                              for label, (calls, elapsed, own) in local.times.items()}}
            with target.open("a", encoding="utf-8") as out:
                print(json.dumps(row, ensure_ascii=True), file=out)
    service.dispatch = measured

    # This work runs on a separate thread concurrently with recall. Distinct file
    # and clock interval keep it out of all exclusive sums.
    count = _TokenSession.count
    def count_with_warmup(self, text):
        if text or not threading.current_thread().name.startswith("evernook-tokenizer-warmup"):
            return count(self, text)
        start = time.perf_counter_ns()
        try:
            return count(self, text)
        finally:
            elapsed = time.perf_counter_ns() - start
            with target.with_name("warmup.jsonl").open("a", encoding="utf-8") as out:
                print(json.dumps({"elapsed_ms": elapsed / 1e6}), file=out)
    _TokenSession.count = count_with_warmup


@contextmanager
def owned_service(args):
    endpoint = args.config.parent / service.ENDPOINT
    previous = endpoint.read_bytes() if endpoint.exists() else None
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "server", str(args.output / "server.jsonl"),
               "--config", str(args.config), "--embedding-config", str(args.embedding_config),
               "--context-config", str(args.context_config)]
    if args.activate_owner:
        command.extend(["--activate-owner", str(args.config.parent / "mcp-access-drill-admin.json")])
    with (args.output / "service.log").open("x") as log, subprocess.Popen(command, stdout=log, stderr=log) as proc:
        try:
            deadline = time.monotonic() + 120
            while not endpoint.exists() or endpoint.read_bytes() == previous:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("phase service could not start")
                time.sleep(.1)
            yield
        finally:
            proc.terminate()
            proc.wait(timeout=20)


def run(args):
    from evernook.service import ServiceClient

    reader, writer = ServiceClient(args.config, reuse=True), ServiceClient(args.config, reuse=True)
    actor = reader.access_from_file(args.config.parent / "mcp-access-scale-writer.json")
    if actor.client_id != "synthetic-scale-writer":
        raise ValueError("dedicated synthetic scale client required")
    args.output.mkdir()
    queries = {"historical": "合成条目0000001", "recent": f"合成条目{args.expected - 1:07d}",
               "no_answer": "外星深海音乐会许可证", "popular": "记录"}
    model = json.loads(Path(json.loads(args.context_config.read_bytes())["tokenizer_config"]).read_bytes())["model_id"]
    session = "mixed-phases-" + uuid4().hex
    rows = []
    try:
        with owned_service(args), ThreadPoolExecutor(max_workers=2) as pool:
            if reader.handshake(actor)["entry"] != "synthetic-scale":
                raise RuntimeError("unexpected fixture entry")
            for case, query in queries.items():
                index = len(rows)
                capture = Capture(Event(Source("synthetic-scale", session, "main", str(index)), "user", index,
                                        None, "complete", f"合成并发写入样本 {index}。"), "message")
                barrier = threading.Barrier(2)
                def timed(call):
                    barrier.wait(timeout=10)
                    start = time.perf_counter()
                    value = call()
                    end = time.perf_counter()
                    return value, start, end
                write = pool.submit(timed, lambda: writer.submit_capture(actor, capture))
                bundle, r0, r1 = timed(lambda: reader.context(actor, query, model_id=model, max_tokens=4096,
                                                      as_of="2026-11-04T00:00:00Z", timeout=120))
                receipt, w0, w1 = write.result()
                if not receipt.message.original_saved or not max(r0, w0) < min(r1, w1):
                    raise RuntimeError("paired request did not overlap or capture was not acknowledged")
                trace = json.loads((args.output / "server.jsonl").read_text(encoding="utf-8").splitlines()[index])
                warmup = json.loads((args.output / "warmup.jsonl").read_text(encoding="utf-8").splitlines()[index])
                if trace["query"] != query or trace["semantic"]["space_id"] != LocalMiniLM.space.key:
                    raise RuntimeError("wrong query trace or embedding space")
                phases = trace["stages"]
                dispatch_ms = phases["dispatch"]["inclusive_ms"]
                if abs(dispatch_ms - sum(s["exclusive_ms"] for s in phases.values())) > .001:
                    raise RuntimeError("exclusive stages do not cover the dispatch interval")
                payload = json.loads(bundle["payload"]) if bundle["payload"] else {}
                evidence = [item["data"] for item in payload.get("items", []) if item["kind"] == "evidence"]
                target = {"historical": 1, "recent": args.expected - 1}.get(case)
                expected_source = asdict(Source("synthetic-scale", "zh-scale-v1", "main", f"m{target:07d}")) if target is not None else None
                row = {"case": case, "client_ms": (r1-r0)*1000, "capture_ms": (w1-w0)*1000,
                       "outside_dispatch_ms": (r1-r0)*1000-dispatch_ms, "parallel_tokenizer_warmup_ms": warmup["elapsed_ms"],
                       "pair_timestamps": {"read_start": r0, "read_end": r1, "write_start": w0, "write_end": w1},
                       "complete": bundle["complete"], "degraded": bundle["degraded"], "evidence_items": len(evidence),
                       "target_present": None if target is None else any(item["evidence"]["evidence"]["source"] == expected_source
                                      and query in item["excerpt"]["text"] for item in evidence), **trace}
                rows.append(row)
                report = {"installed_version": version("evernook"), "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          "host_model": model, "max_tokens": 4096, "samples": rows}
                (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"case": case, "client_ms": row["client_ms"], "backend": trace["backend"]}), flush=True)
    finally:
        reader.close()
        writer.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["server"]:
        install_trace(Path(sys.argv[2]))
        del sys.argv[1:3]
        service.main()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for name in ("config", "embedding-config", "context-config", "output"):
            parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--activate-owner", action="store_true",
                            help="Activate an unowned synthetic clone using its paired drill admin")
        parser.add_argument("--expected", type=int, default=1_000_000)
        run(parser.parse_args())
