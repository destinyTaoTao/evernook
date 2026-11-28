"""Measure recovery to the first exact service read after loss of a synthetic ledger."""
from __future__ import annotations

from importlib.metadata import version
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.backup import create_backup, initialize_backup_keys
from evernook.offline_recovery import ACKNOWLEDGEMENT, OfflineRecovery
from evernook.service import ENDPOINT, ServiceClient


def drill(root: Path) -> dict[str, object]:
    config, owner = initialize(root / "source", client_id="fixture", entry="fixture", allow_archive_review=True)
    memory = MemoryRuntime(config)
    password = secrets.token_bytes(32)
    keys = initialize_backup_keys(root / "keys", password)
    events = [Event(Source("fixture", "snapshot-drill", "main", str(i)), "user", i, None,
                    "complete", f"合成恢复记录 {i}。\r\n🙂e\u0301") for i in range(1, 7)]
    receipts = [memory.submit(owner, event) for event in events[:5]]
    memory.change(owner, Change("before-snapshot", receipts[0].message_id, 1, "suppress",
                                receipts[3].message_id, "synthetic control"))
    snapshot = create_backup(memory, owner, root / "backup", root / "keys")
    late = memory.submit(owner, events[5])
    memory.change(owner, Change("after-snapshot", receipts[1].message_id, 1, "suppress",
                                late.message_id, "synthetic control"))
    if memory.read(owner, receipts[1].message_id) is not None:
        raise RuntimeError("source did not apply the post-snapshot control")
    # Only the drill's own source directory is moved. Recovery gets the backup and key.
    config.parent.rename(root / "source-offline")
    destination = root / "restored"
    started = time.perf_counter()
    with OfflineRecovery(root / "backup", destination, Path(keys.recovery_file), password) as recovery:
        review = recovery.review()
        if review["later_changes"] != "unknown" or destination.exists() or review["messages"] != 5:
            raise RuntimeError("unexpected recovery preview")
        recovery.publish(ACKNOWLEDGEMENT)
    published = time.perf_counter()
    restored_config = destination / "runtime.json"
    owner_file = destination / "mcp-access-owner.json"
    client = ServiceClient(restored_config)
    access = client.access_from_file(owner_file)
    service = subprocess.Popen(
        [sys.executable, "-m", "evernook.service", "--config", str(restored_config),
         "--activate-owner", str(owner_file)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while not (destination / ENDPOINT).exists():
            if service.poll() is not None or time.perf_counter() - published > 30:
                raise RuntimeError("restored service did not start")
            time.sleep(0.025)
        first = client.read(access, receipts[2].message_id)
        if first is None or first.event != events[2] or first.receipt != receipts[2]:
            raise RuntimeError("first exact read or receipt differed")
        readable = time.perf_counter()
        if client.read(access, receipts[0].message_id) is not None or client.read(access, late.message_id) is not None:
            raise RuntimeError("snapshot read exposed a pre-snapshot suppression or a post-snapshot addition")
        for event, receipt in zip(events[1:5], receipts[1:5], strict=True):
            message = client.read(access, receipt.message_id)
            if message is None or message.event != event or message.receipt != receipt:
                raise RuntimeError("snapshot source or receipt differed")
        try:
            client.read(owner, receipts[2].message_id)
        except MemoryFailure as failure:
            if failure.code != "unauthorized":
                raise
        else:
            raise RuntimeError("old owner credentials remained valid")
    finally:
        service.terminate()
        service.communicate(timeout=15)
        client.close()
    return {"version": version("evernook"), "snapshot_created_at": snapshot.created_at,
            "backup_id": snapshot.backup_id, "snapshot_rows": len(receipts), "visible_exact_receipts": 4,
            "pre_snapshot_suppression_preserved": True, "post_snapshot_additions_missing": 1,
            "post_snapshot_suppressions_missing": 1, "later_changes": review["later_changes"],
            "old_owner_rejected": True, "preview_and_publish_ms": round((published - started) * 1000, 1),
            "restore_to_first_verified_service_read_ms": round((readable - started) * 1000, 1)}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="evernook-snapshot-drill-") as directory:
        print(json.dumps(drill(Path(directory)), indent=2))
