"""Exercise explicit launcher selection between isolated installed-version/data pairs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

from evernook import Event, ImportantMark, MemoryRuntime, Source, initialize
from evernook.archive import ControlCheckpoint, capture_control_checkpoint, replay_archive_controls
from evernook.paths import external
from evernook.runtime_restore import publish_replayed_runtime


ORIGINAL = "合成回滚保留原文：37.50元，🙂。"


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def select(root: Path, value: object) -> None:
    pending = root / "launcher.next.json"
    save(pending, value)
    pending.replace(root / "launcher.json")


def observe_selected(root: Path) -> dict[str, object]:
    selected = json.loads((root / "launcher.json").read_text(encoding="utf-8"))
    result = subprocess.run([selected["python"], str(Path(__file__).resolve()), "observe", "--output", str(root)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=root,
        env={**os.environ, "PYTHONPATH": ""}, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("selected_environment_probe_failed")
    observed: dict[str, object] = json.loads(result.stdout)
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "exercise", "observe"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    if args.phase == "prepare":
        if importlib.metadata.version("evernook") != "0.1.0":
            raise ValueError("old_installed_environment_required")
        root.mkdir()
        config, owner = initialize(root / "previous-runtime", client_id="synthetic-previous", entry="installed-rollback", allow_archive_review=True)
        runtime = MemoryRuntime(config)
        receipt = runtime.submit(owner, Event(Source("installed-rollback", "synthetic", "main", "original"), "user", 0, None, "complete", ORIGINAL))
        important = runtime.mark_important(owner, ImportantMark(receipt.message_id, "original", 1, "synthetic", None, None, ORIGINAL))
        owner_file = config.parent / "mcp-access-selected-owner.json"
        save(owner_file, asdict(owner))
        runtime.export_archive(owner, root / "snapshot")
        checkpoint = capture_control_checkpoint(runtime, owner)
        save(config.parent / "checkpoint.json", asdict(checkpoint))
        replay_archive_controls(runtime, owner, root / "snapshot", root / "review", checkpoint=checkpoint)
        previous = {"python": sys.executable, "runtime": str(config), "access": str(owner_file),
            "original_id": receipt.message_id, "important_id": important.important_id}
        save(root / "previous.json", previous)
        select(root, previous)
        return 0
    if args.phase == "observe":
        selected = json.loads((root / "launcher.json").read_text(encoding="utf-8"))
        runtime = MemoryRuntime(Path(selected["runtime"]))
        owner = runtime.access_from_file(Path(selected["access"]))
        original = runtime.read(owner, selected["original_id"])
        print(json.dumps({"version": importlib.metadata.version("evernook"),
            "exact": original is not None and original.event.text == ORIGINAL,
            "important_exact": runtime.read_important(owner, selected["important_id"]).text == ORIGINAL}))
        return 0
    if importlib.metadata.version("evernook") != "0.1.1":
        raise ValueError("fixed_installed_candidate_required")
    previous = json.loads((root / "previous.json").read_text(encoding="utf-8"))
    before = observe_selected(root)
    runtime = MemoryRuntime(Path(previous["runtime"]))
    owner = runtime.access_from_file(Path(previous["access"]))
    checkpoint = ControlCheckpoint(**json.loads((runtime.root / "checkpoint.json").read_text(encoding="utf-8")))
    config, candidate_owner = publish_replayed_runtime(runtime, owner, root / "review", root / "candidate-runtime",
        checkpoint=checkpoint, client_id="synthetic-candidate", entry="installed-rollback")
    candidate_file = config.parent / "mcp-access-selected-owner.json"
    save(candidate_file, asdict(candidate_owner))
    select(root, {**previous, "python": sys.executable, "runtime": str(config), "access": str(candidate_file)})
    candidate_observation = observe_selected(root)
    candidate = MemoryRuntime(config)
    extra_event = Event(Source("installed-rollback", "synthetic", "main", "candidate-only"), "user", 1, None, "complete",
        "合成候选新增记录，回滚后仍保留在候选副本。")
    extra = candidate.submit(candidate_owner, extra_event)
    # Explicit operator selection of the preserved pre-upgrade evidence pair, never a binary downgrade on used candidate data.
    select(root, previous)
    rolled_back = observe_selected(root)
    preserved = candidate.read(candidate_owner, extra.message_id)
    checks = {"previous_installed_version": before["version"] == "0.1.0" and before["exact"] is True and before["important_exact"] is True,
        "candidate_installed_version": candidate_observation["version"] == "0.1.1" and candidate_observation["exact"] is True,
        "candidate_important_exact": candidate_observation["important_exact"] is True,
        "rollback_selected_previous_version": rolled_back["version"] == "0.1.0",
        "rollback_original_and_important_exact": rolled_back["exact"] is True and rolled_back["important_exact"] is True,
        "inactive_candidate_data_retained": preserved is not None and preserved.event.text == extra_event.text,
        "preupgrade_runtime_has_no_candidate_only_record": runtime.read_source(owner, extra_event.source) is None}
    save(root / "report.json", {"checks": checks, "before": before, "candidate": candidate_observation, "rolled_back": rolled_back,
        "scope": "Actual isolated test-launcher selection of installed Python and matching runtime pair. No user launcher changed; rollback chooses pre-upgrade data, candidate-only additions remain in the retained inactive copy and are not active. No downgrade on newly used control state."})
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
