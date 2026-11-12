"""Installed independent service: real idle embedding and long-input recall, synthetic only."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import time

import evernook
from evernook import Event, MemoryRuntime, Source, initialize
from evernook.paths import external
from evernook.scheduler import ConsolidationScheduler
from evernook.service import ServiceClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    root.mkdir()
    config, access = initialize(root / "runtime", client_id="synthetic", entry="fixture")
    access_path = config.parent / "mcp-access-owner.json"
    access_path.write_text(json.dumps(asdict(access)), encoding="utf-8")
    ConsolidationScheduler(MemoryRuntime(config)).configure(access, min_pending=1, idle_seconds=0, max_trigger_seconds=1)
    process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service", "--config", str(config),
        "--activate-owner", str(access_path), "--maintenance-access", str(access_path),
        "--maintenance-interval", "0.2", "--embedding-config", str(args.embedding_config)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not (config.parent / "memory-service.json").exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("semantic_service_start_failed")
            time.sleep(.05)
        client = ServiceClient(config)
        receipt = client.call(access, "remember", {"event": asdict(Event(Source("fixture", "history", "main", "1"),
            "user", 1, "2019-01-01T00:00:00Z", "complete", "合成：我喜欢辛辣的食物。"))})
        deadline = time.monotonic() + 30
        while True:
            status = client.call(access, "status", {})
            semantic = status["service"]["maintenance"].get("semantic")
            if isinstance(semantic, dict) and semantic.get("state") == "ready" and semantic.get("stored_vectors") == 1:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("idle_semantic_index_not_ready")
            time.sleep(.1)
        query = "无关背景。" * 100 + "饮食偏好？"
        page = client.call(access, "recall", {"query": query, "query_intent": "辛辣"})
        if (not any(hit["evidence"]["message_id"] == receipt["message_id"] for hit in page["hits"])
                or page["semantic"]["state"] != "ready"
                or page["query_plan"]["kind"] != "explicit_intent"
                or page["semantic_search_backend"] != "sqlite-vec-0.1.9-exact"):
            raise RuntimeError("installed_long_recall_failed")
        modules = Path(evernook.__file__).parent
        report = {"version": version("evernook"), "package_root": str(modules),
            "module_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in modules.glob("*.py")},
            "receipt": receipt, "idle_status": status, "recall": page,
            "scope": "Actual installed service and pinned embedding; no dsh host present during maintenance. One synthetic source; no scale or daily admission.",
            "admission": "pending"}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)
        if process.stderr:
            process.stderr.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
