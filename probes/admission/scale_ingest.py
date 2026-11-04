"""Prepare a real scale fixture through the public API; preserve partial evidence on failure."""
from __future__ import annotations

import argparse
from array import array
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import time
from typing import Any

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from evernook.paths import external


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak_working", "working", "peak_paged", "paged", "peak_nonpaged",
                                            "nonpaged", "pagefile", "peak_commit", "private")]


def memory_sample() -> dict[str, int] | None:
    if platform.system() != "Windows":
        return None
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    query.restype = wintypes.BOOL
    if not query(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
        return None
    return {"private_bytes": int(counters.private), "peak_commit_bytes": int(counters.peak_commit),
            "working_set_bytes": int(counters.working), "peak_working_set_bytes": int(counters.peak_working)}


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--messages", type=int, choices=(100000, 1000000), required=True)
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("policy.json"))
    args = parser.parse_args()
    policy_raw = args.policy.read_bytes()
    policy: dict[str, Any] = json.loads(policy_raw)
    if policy["schema_version"] != 1 or args.messages not in policy["scale"]["message_counts"]:
        raise ValueError("invalid_experiment_policy")
    root = external(args.output)
    root.mkdir()
    checkout = Path(__file__).resolve().parents[2]
    hashes = {str(path.relative_to(checkout)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted((checkout / "src" / "evernook").iterdir()) if path.suffix in (".py", ".mjs")}
    manifest = {"phase": "public_scale_ingestion", "expected_messages": args.messages,
                "fixture_version": policy["scale"]["fixture_version"],
                "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
                "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "core_sha256": hashes,
                "source_baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(),
                "python": platform.python_version(), "platform": platform.platform(), "byteorder": "little",
                "latencies": "submit-ms.f64le contains every public submit call in source order",
                "resource_scope": "owned ingestion process, no child processes; model/host phases reported separately",
                "concurrency": "other explicitly recorded isolated benchmark processes may share this machine"}
    save(root / "manifest.json", manifest)
    save(root / "policy.json", policy)
    config, _owner = initialize(root / "runtime", client_id="synthetic-owner", entry="owner")
    actor = pair_client(config, client_id="synthetic-scale-writer", entry="synthetic-scale",
                        access_path=config.parent / "mcp-access-scale-writer.json", scopes=("capture", "recall"))
    runtime = MemoryRuntime(config)
    started = time.perf_counter()
    samples = array("d")
    checksum = hashlib.sha256()
    receipts: dict[str, object] = {}
    completed = 0
    error: str | None = None
    resource: dict[str, int] | None = None
    topics = ("湖边步道观察记录", "厨房食材库存记录", "天文摄影曝光记录", "编辑器快捷键练习",
              "图书馆书架整理记录", "公交时刻抄录练习", "花园植物浇水记录", "散步路线观察笔记")
    first = datetime(2021, 1, 1, tzinfo=timezone.utc)
    try:
        with (root / "submit-ms.f64le").open("wb") as raw, (root / "progress.jsonl").open("w", encoding="utf-8") as progress:
            for index in range(args.messages):
                text = policy["scale"]["pure_semantic_original"] if index == 0 else (
                    f"合成条目{index:07d}：{topics[index % len(topics)]}；保留数字37.50与🙂。")
                when = first + timedelta(days=index % 1461) if index < args.messages * 3 // 4 else datetime(2026, 11, 3, tzinfo=timezone.utc)
                event = Event(Source("synthetic-scale", "zh-scale-v1", "main", f"m{index:07d}"),
                              "user", index, when.isoformat(), "complete", text)
                checksum.update(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
                before = time.perf_counter()
                receipt = runtime.submit(actor, event)
                samples.append((time.perf_counter() - before) * 1000)
                if not receipt.original_saved:
                    raise ValueError("synthetic_original_not_saved")
                completed += 1
                if index in (0, 1, args.messages // 2, args.messages - 1):
                    restored = runtime.read(actor, receipt.message_id)
                    if restored is None or restored.event != event:
                        raise ValueError("synthetic_source_roundtrip_failed")
                    receipts[str(index)] = asdict(receipt)
                if completed % 1000 == 0 or completed == args.messages:
                    samples[-min(1000, len(samples)):].tofile(raw)
                    raw.flush()
                    resource = memory_sample()
                    record = {"messages": completed, "elapsed_seconds": time.perf_counter() - started, "memory": resource}
                    progress.write(json.dumps(record, ensure_ascii=False) + "\n")
                    progress.flush()
                    print(json.dumps(record, ensure_ascii=False), flush=True)
                    if record["elapsed_seconds"] > policy["scale"]["ingest_seconds_max"]:
                        error = "ingest_time_budget"
                        break
                    if resource and resource["peak_commit_bytes"] > policy["resources"]["owned_private_bytes_max"]:
                        error = "ingest_resource_budget"
                        break
    except Exception as failure:
        error = str(failure) if str(failure) in ("synthetic_original_not_saved", "synthetic_source_roundtrip_failed") else "ingest_failed"
    if len(samples) % 1000:
        with (root / "submit-ms.f64le").open("ab") as raw:
            samples[-(len(samples) % 1000):].tofile(raw)
    actual = runtime.status(actor).messages
    ordered = sorted(samples)
    report = {"phase": "public_scale_ingestion", "admission": "pending", "expected_messages": args.messages,
              "completed": completed, "actual_messages": actual, "elapsed_seconds": time.perf_counter() - started,
              "submit_p50_ms": ordered[(len(ordered) * 50 + 99) // 100 - 1] if ordered else None,
              "submit_p95_ms": ordered[(len(ordered) * 95 + 99) // 100 - 1] if ordered else None,
              "sample_count": len(ordered), "attempted_fixture_sha256": checksum.hexdigest(), "memory": resource,
              "runtime_and_evidence_bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
              "selected_receipts": receipts, "error_code": error, "writer_scopes": ["capture", "recall"]}
    save(root / "ingest-report.json", report)
    return 0 if error is None and completed == actual == args.messages else 2


if __name__ == "__main__":
    raise SystemExit(main())
