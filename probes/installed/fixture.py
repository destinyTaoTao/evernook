"""Prepare an isolated installed-wheel fixture; never emit launcher credentials."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import time

from evernook import Event, ImportantMark, MemoryRuntime, Source, initialize, pair_client
from evernook.maintenance import MaintenanceBudget, NavigationMaintenance, NavigationPolicy
from evernook.paths import external


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "maintain"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    root = external(args.output)
    if args.phase == "prepare":
        root.mkdir()
        config, owner = initialize(root / "runtime", client_id="synthetic-installed-owner", entry="installed-fixture", allow_archive_review=True)
        runtime = MemoryRuntime(config)
        # Explicit trusted launcher credential in the already private runtime directory.
        save(config.parent / "mcp-access-owner.json", asdict(owner))
        for entry in ("installed-harness", "dsh", "pi"):
            pair_client(config, client_id="synthetic-" + entry, entry=entry,
                        access_path=config.parent / ("mcp-access-" + entry + ".json"), scopes=("capture", "recall"))
        maintenance = pair_client(config, client_id="synthetic-maintenance", entry="installed-maintenance",
            access_path=config.parent / "mcp-access-maintenance.json", scopes=("maintain", "recall"))
        original = "合成安装重要原话：37.50元，🙂和e\u0301，保留\r\n换行。"
        receipt = runtime.submit(owner, Event(Source("installed-fixture", "synthetic", "main", "important"),
            "user", 0, "2026-11-04T00:00:00Z", "complete", original))
        item = runtime.mark_important(owner, ImportantMark(receipt.message_id, "original", 1, "synthetic", None, None, original))
        runtime.refresh_lexical(maintenance)
        save(root / "fixture.json", {"config": str(config), "original": original, "original_id": receipt.message_id,
             "important_id": item.important_id, "core_version": importlib.metadata.version("evernook"),
             "nacl_available": importlib.util.find_spec("nacl") is not None,
             "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "scope": "Synthetic public installed-wheel fixture; vault remains disabled, no model configured."})
        return
    runtime = MemoryRuntime(root / "runtime/runtime.json")
    access = runtime.access_from_file(root / "runtime/mcp-access-maintenance.json")
    navigation = NavigationMaintenance(runtime)
    policy = NavigationPolicy(0)
    navigation.configure(access, policy)
    started = time.perf_counter()
    with (root / "maintenance.jsonl").open("x", encoding="utf-8") as log:
        while time.perf_counter() - started < args.seconds and not (root / "maintenance-stop").exists():
            before = time.perf_counter()
            lexical = runtime.refresh_lexical(access, limit=100)
            tick = navigation.tick(access, policy.policy_id, budget=MaintenanceBudget(64, 4, 2))
            log.write(json.dumps({"elapsed_seconds": time.perf_counter() - started,
                "lexical": asdict(lexical), "navigation": asdict(tick)}, ensure_ascii=False) + "\n")
            log.flush()
            time.sleep(max(0, 2 - (time.perf_counter() - before)))


if __name__ == "__main__":
    main()
