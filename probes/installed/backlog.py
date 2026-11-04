"""Paused navigation backlog and installed-process catch-up with foreground traffic."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from evernook import Event, MemoryRuntime, Source, initialize, pair_client
from evernook.maintenance import MaintenanceBudget, NavigationMaintenance, NavigationPolicy
from evernook.paths import external


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("exercise", "drain"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    policy = NavigationPolicy(0)
    if args.phase == "drain":
        runtime = MemoryRuntime(root / "runtime/runtime.json")
        maintainer = runtime.access_from_file(root / "runtime/mcp-access-maintenance.json")
        navigation = NavigationMaintenance(runtime)
        initially_paused = navigation.status(maintainer, policy.policy_id).paused
        navigation.pause(maintainer, policy.policy_id, paused=False)
        started = time.perf_counter()
        ticks: list[dict[str, object]] = []
        converged = False
        while len(ticks) < 64 and time.perf_counter() - started < 60:
            begin = time.perf_counter_ns()
            tick = navigation.tick(maintainer, policy.policy_id, budget=MaintenanceBudget(16, 1, 2))
            status = navigation.status(maintainer, policy.policy_id)
            ticks.append({"begin_ns": begin, "end_ns": time.perf_counter_ns(), "tick": asdict(tick), "status": asdict(status)})
            converged = ((root / "foreground-finished").exists() and status.message_cursor == status.observed_message_head
                         and status.queued == 0 and status.running == 0 and status.succeeded > 0)
            save(root / "drain.json", {"initially_paused": initially_paused, "converged": converged, "ticks": ticks})
            if converged:
                break
            time.sleep(.05)
        return 0 if initially_paused and converged else 1
    root.mkdir()
    config, _owner = initialize(root / "runtime", client_id="synthetic-owner", entry="owner")
    writer = pair_client(config, client_id="synthetic-backlog-writer", entry="installed-backlog",
        access_path=config.parent / "mcp-access-writer.json", scopes=("capture", "recall"))
    maintainer = pair_client(config, client_id="synthetic-backlog-maintenance", entry="installed-maintenance",
        access_path=config.parent / "mcp-access-maintenance.json", scopes=("maintain", "recall"))
    runtime = MemoryRuntime(config)
    navigation = NavigationMaintenance(runtime)
    navigation.configure(maintainer, policy)
    navigation.pause(maintainer, policy.policy_id, paused=True)
    for index in range(192):
        runtime.submit(writer, Event(Source("installed-backlog", "synthetic", "main", "seed:" + str(index)), "user", index,
            "2026-11-04T00:00:00Z", "complete", f"合成积压{index}：37.50元。"))
    before = navigation.status(maintainer, policy.policy_id)
    save(root / "before.json", asdict(before))
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "drain", "--output", str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    foreground = []
    for index in range(10):
        begin = time.perf_counter_ns()
        text = f"合成追赶时前台{index}：🙂。"
        receipt = runtime.submit(writer, Event(Source("installed-backlog", "synthetic", "main", "foreground:" + str(index)),
            "user", 192 + index, "2026-11-04T00:00:00Z", "complete", text))
        message = runtime.read(writer, receipt.message_id)
        foreground.append({"begin_ns": begin, "end_ns": time.perf_counter_ns(),
            "exact": message is not None and message.event.text == text})
        time.sleep(.1)
    (root / "foreground-finished").write_text("finished\n", encoding="utf-8")
    _, stderr = process.communicate(timeout=120)
    drain = json.loads((root / "drain.json").read_text(encoding="utf-8"))
    overlap = any(min(front["end_ns"], tick["end_ns"]) > max(front["begin_ns"], tick["begin_ns"])
                  for front in foreground for tick in drain["ticks"])
    checks = {"paused_backlog_192": before.paused and before.observed_message_head - before.message_cursor >= 192,
        "separate_installed_process": process.returncode == 0, "paused_state_survives_restart": drain["initially_paused"],
        "bounded_navigation_converged": drain["converged"], "multiple_bounded_ticks": len(drain["ticks"]) > 1,
        "foreground_exact": all(item["exact"] for item in foreground), "actual_intervals_overlap": overlap}
    save(root / "report.json", {"checks": checks, "foreground": foreground,
        "final": drain["ticks"][-1]["status"], "child_error": stderr.decode("utf-8", errors="replace")[-2048:],
        "scope": "Installed public navigation task backlog only. Generic derived/model jobs are not claimed drained. Perf-counter intervals share the Windows monotonic clock."})
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
