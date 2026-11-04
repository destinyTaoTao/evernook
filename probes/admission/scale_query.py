"""Public core latency matrix with real concurrent capture or derived-index maintenance."""
from __future__ import annotations

import argparse
import cProfile
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from types import CodeType

from evernook import Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source
from evernook.models import Access
from evernook.semantic import RecallPage
from evernook.host_context import HostContext
from evernook.paths import external
from scale_ingest import save


class MeasuredMiniLM(LocalMiniLM):
    total_embed_ms = 0.0

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        started = time.perf_counter()
        try:
            return super().embed(texts, timeout=timeout, cancel=cancel)
        finally:
            self.total_embed_ms += (time.perf_counter() - started) * 1000


class MeasuredRecall(SemanticRecall):
    def __init__(self, runtime: MemoryRuntime, backend: MeasuredMiniLM):
        super().__init__(runtime, backend)
        self.measured_backend = backend
        self.last_phases: dict[str, float] | None = None

    def recall(self, access: Access, query: str, **kwargs: Any) -> RecallPage:
        started = time.perf_counter()
        embedded_before = self.measured_backend.total_embed_ms
        try:
            return super().recall(access, query, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            embedded = self.measured_backend.total_embed_ms - embedded_before
            self.last_phases = {"recall_total_ms": elapsed, "embedding_inclusive_ms": embedded,
                "retrieval_fusion_source_excluding_embedding_ms": elapsed - embedded}


def measure(args: argparse.Namespace, policy: dict[str, Any]) -> None:
    runtime = MemoryRuntime(args.fixture / "runtime" / "runtime.json")
    actor = runtime.access_from_file(args.fixture / "runtime" / "mcp-access-scale-writer.json")
    configured = LocalMiniLM.from_file(args.embedding_config)
    recall = MeasuredRecall(runtime, MeasuredMiniLM(node=configured.node,
        transformers_dir=configured.transformers_dir, model_dir=configured.model_dir))
    if args.cell == "pure_semantic":
        query = policy["scale"]["pure_semantic_query"]
        target = runtime.read_source(actor, Source("synthetic-scale", "zh-scale-v1", "main", "m0000000"))
        started = time.perf_counter()
        page = recall.recall(actor, query, limit=10, as_of=policy["scale"]["as_of"], timeout=120)
        save(args.result, {"query": query, "recall_ms": (time.perf_counter() - started) * 1000,
             "page": asdict(page), "target_message_id": target.receipt.message_id if target else None,
             "target_retrieved": bool(target and any(hit.evidence.message_id == target.receipt.message_id for hit in page.hits)),
             "incomplete_evidence": bool(page.degraded or page.semantic_scan_limited),
             "scan_limit_observed": (page.semantic.stored_vectors or 0) > 10000 and page.semantic_scan_limited
                                    and "semantic_scan_limit" in page.degraded,
             "scope": "Candidates and bounded coverage only; neither an empty result nor a miss proves historical absence."})
        return
    context = HostContext(runtime, recall, args.context_config)
    model_id = context.status()["model_id"]
    fixture = json.loads((args.fixture / "manifest.json").read_text(encoding="utf-8"))
    last_index = fixture["expected_messages"] - 1
    recent = runtime.read_source(actor, Source("synthetic-scale", "zh-scale-v1", "main", f"m{last_index:07d}"))
    if recent is None or recent.event.occurred_at != "2026-11-03T00:00:00+00:00":
        raise ValueError("recent_fixture_target_missing_or_not_recent")
    queries = {"recent": f"合成条目{last_index:07d}", "historical": "合成条目0000001",
               "no_answer": "外星深海音乐会许可证", "popular": "记录"}
    query = queries[args.cell]
    warmup_ms = None
    if args.warmup:
        started = time.perf_counter()
        runtime.search(actor, query, since="2026-11-03T00:00:00Z" if args.cell == "recent" else None, limit=10)
        context.build(actor, query, model_id=model_id, max_tokens=policy["host"]["memory_tokens"],
                      as_of=policy["scale"]["as_of"], mode="normal", timeout=120)
        warmup_ms = (time.perf_counter() - started) * 1000
    rows = []
    for index in range(args.samples):
        profiler = cProfile.Profile() if args.profile and index == 0 else None
        before = time.perf_counter()
        if profiler:
            profiler.enable()
        lexical = runtime.search(actor, query, since="2026-11-03T00:00:00Z" if args.cell == "recent" else None, limit=10)
        lexical_ms = (time.perf_counter() - before) * 1000
        started = time.perf_counter()
        recall.last_phases = None
        bundle = context.build(actor, query, model_id=model_id, max_tokens=policy["host"]["memory_tokens"],
                               as_of=policy["scale"]["as_of"], mode="normal", timeout=120)
        context_ms = (time.perf_counter() - started) * 1000
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(args.result.with_suffix(".prof")))
            phases = []
            for statistic in profiler.getstats():
                code = statistic.code
                if isinstance(code, CodeType) and code.co_name in {"_authorize", "_audit", "embed", "_semantic", "search", "read", "state", "recall", "build", "count"}:
                    phases.append({"file": Path(code.co_filename).name, "line": code.co_firstlineno, "function": code.co_name,
                         "calls": statistic.callcount, "self_ms": statistic.inlinetime * 1000, "inclusive_ms": statistic.totaltime * 1000})
            save(args.result.with_suffix(".phases.json"), {"phases": phases,
                "scope": "separate diagnostic sample; inclusive phases overlap and must not be added; embed includes integrity, process/model startup and inference"})
        rows.append({"lexical_ms": lexical_ms, "context_ms": context_ms, "profiled": profiler is not None,
                     "lexical": asdict(lexical), "context": asdict(bundle), "recall_phases": recall.last_phases})
        save(args.result, {"cell": args.cell, "unscored_warmup_ms": warmup_ms, "samples": rows})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--context-config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cell", choices=("recent", "historical", "no_answer", "popular", "pure_semantic"))
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--warmup", action="store_true")
    args = parser.parse_args()
    policy_raw = Path(__file__).with_name("policy.json").read_bytes()
    policy = json.loads(policy_raw)
    if args.cell:
        measure(args, policy)
        return 0
    root = external(args.output)
    root.mkdir()
    runtime = MemoryRuntime(args.fixture / "runtime" / "runtime.json")
    reader = runtime.access_from_file(args.fixture / "runtime" / "mcp-access-scale-writer.json")
    maintainer = runtime.access_from_file(args.fixture / "runtime" / "mcp-access-scale-maintenance.json")
    semantic = SemanticRecall(runtime, LocalMiniLM.from_file(args.embedding_config))
    save(root / "manifest.json", {"fixture": str(args.fixture), "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "before_messages": runtime.status(reader).messages,
        "semantic_before": asdict(semantic.status(reader)), "cold": policy["scale"]["cold_definition"],
        "warm": policy["scale"]["warm_definition"], "context_config_sha256": hashlib.sha256(args.context_config.read_bytes()).hexdigest(),
        "scope": "Core public API matrix; Python cold host, not native SDK IPC. Other recorded experiments share the machine."})
    results = []
    traffic: list[dict[str, Any]] = []
    for condition in policy["scale"]["conditions"]:
        stop = threading.Event()
        ready = threading.Event()
        counters: dict[str, Any] = {"condition": condition, "completed": 0, "noops": 0, "errors": []}

        def background() -> None:
            while not stop.is_set():
                try:
                    if condition == "writing":
                        index = counters["completed"]
                        receipt = runtime.submit(reader, Event(Source("synthetic-scale", root.name, condition, str(index)), "user", index,
                            "2026-11-04T00:00:00Z", "complete", f"并发合成写入{index}：散步观察记录。"))
                        if not receipt.original_saved:
                            raise ValueError("background_original_not_saved")
                    else:
                        lexical_before = runtime.lexical_status(maintainer)
                        semantic_before = semantic.status(maintainer)
                        # Start another real rebuild when coverage is complete; do not count ready-state polling.
                        if counters["completed"] == 0 or lexical_before.pending_messages == 0:
                            lexical_after = runtime.rebuild_lexical(maintainer, limit=100)
                        else:
                            lexical_after = runtime.refresh_lexical(maintainer, limit=100)
                        semantic_after = semantic.refresh(maintainer, limit=16, timeout=120)
                        if (lexical_before.indexed_messages == lexical_after.indexed_messages
                                and semantic_before.stored_vectors == semantic_after.stored_vectors
                                and semantic_before.indexed_messages == semantic_after.indexed_messages):
                            counters["noops"] += 1
                            ready.set()
                            stop.wait(.25)
                            continue
                    counters["completed"] += 1
                    ready.set()
                except Exception as error:
                    counters["errors"].append(type(error).__name__)
                    ready.set()
                    stop.wait(.25)

        thread = None
        if condition != "idle":
            thread = threading.Thread(target=background)
            thread.start()
            ready.wait(timeout=180)
        try:
            for cell in policy["scale"]["cells"]:
                for cache, count in (("warm", 1), ("fresh_process", policy["scale"]["fresh_process_samples_per_cell"])):
                    for repetition in range(count):
                        result = root / f"{condition}-{cell}-{cache}-{repetition}.json"
                        command = [sys.executable, str(Path(__file__).resolve()), "--fixture", str(args.fixture),
                            "--embedding-config", str(args.embedding_config), "--context-config", str(args.context_config),
                            "--cell", cell, "--samples", str(policy["scale"]["warm_samples_per_cell"] if cache == "warm" else 1),
                            "--result", str(result)]
                        if cache == "warm":
                            command.append("--warmup")
                        started = time.perf_counter()
                        background_before = counters["completed"]
                        errors_before = len(counters["errors"])
                        attempt = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=7200,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        item = {"condition": condition, "cell": cell, "cache": cache, "result": result.name,
                                "process_ms": (time.perf_counter() - started) * 1000, "returncode": attempt.returncode,
                                "background_completed_start": background_before, "background_completed_end": counters["completed"],
                                "background_new_errors": len(counters["errors"]) - errors_before,
                                "valid_workload": condition == "idle" or (counters["completed"] > background_before
                                    and len(counters["errors"]) == errors_before and background_before > 0)}
                        if attempt.returncode:
                            save(result.with_suffix(".failure.json"), {"stderr": attempt.stderr[-4096:], "returncode": attempt.returncode})
                        results.append(item)
                        save(root / "progress.json", {"results": results, "traffic": traffic + [counters]})
                        print(json.dumps(item), flush=True)
                # Profiler overhead is explicitly excluded from the measured samples.
                result = root / f"{condition}-{cell}-diagnostic.json"
                command = [sys.executable, str(Path(__file__).resolve()), "--fixture", str(args.fixture),
                    "--embedding-config", str(args.embedding_config), "--context-config", str(args.context_config),
                    "--cell", cell, "--result", str(result), "--profile"]
                subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            stop.set()
            if thread:
                thread.join()
            traffic.append(counters)
            save(root / "progress.json", {"results": results, "traffic": traffic})
    save(root / "report.json", {"results": results, "traffic": traffic, "after_messages": runtime.status(reader).messages,
                               "semantic_after": asdict(semantic.status(reader))})
    return 0 if all(row["returncode"] == 0 and row["valid_workload"] for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
