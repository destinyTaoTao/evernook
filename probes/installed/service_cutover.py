"""Synthetic-only independent service cutover and bounded restart campaign."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from evernook import Event, MemoryRuntime, Source, initialize
from evernook.models import MemoryFailure
from evernook.scheduler import ConsolidationScheduler
from evernook.service import ServiceClient, client_access


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if not args.output.is_absolute() or not 1 <= args.seconds <= 3600:
        raise ValueError("absolute new output directory and 1..3600 seconds required")
    args.output.mkdir()
    config, owner = initialize(args.output / "data", client_id="synthetic-owner", entry="owner")
    owner_path = config.parent / "mcp-access-owner.json"
    owner_path.write_text(json.dumps(asdict(owner)), encoding="utf-8")
    runtime = MemoryRuntime(config)
    ConsolidationScheduler(runtime).configure(owner, min_pending=1, idle_seconds=0,
        max_trigger_seconds=1)
    first = runtime.submit(owner, Event(Source("owner", "before", "main", "1"),
        "user", 1, None, "complete", "synthetic before service cutover"))
    process: subprocess.Popen[bytes] | None = None
    starts = 0

    def stop() -> None:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)

    def start() -> None:
        nonlocal process, starts
        endpoint = config.parent / "memory-service.json"
        old = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(config), "--activate-owner", str(owner_path),
            "--maintenance-access", str(owner_path), "--maintenance-interval", "0.2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("service readiness failed")
            time.sleep(0.05)
        starts += 1

    started = time.monotonic()
    rounds = 0
    suppressed = 0
    missing_discovery = 0
    progress = args.output / "progress.jsonl"
    try:
        start()
        client = ServiceClient(config)
        assert client.call(owner, "read", {"message_id": first.message_id})["event"]["text"] == "synthetic before service cutover"
        try:
            runtime.status(owner)
        except MemoryFailure as error:
            assert error.code == "service_required"
        else:
            raise AssertionError("old local runtime still active")
        paired = client.call(owner, "pair-client", {"client_id": "synthetic-dsh",
            "entry": "dsh", "scopes": None})
        ordinary = client_access(config, Path(paired["access_file"]))
        while time.monotonic() - started < args.seconds:
            rounds += 1
            text = f"synthetic service campaign item {rounds}"
            event = asdict(Event(Source("dsh", "campaign", "main", str(rounds)), "user", rounds,
                None, "complete", text))
            receipt = client.call(ordinary, "remember", {"event": event})
            assert receipt == client.call(ordinary, "remember", {"event": event})
            assert client.call(ordinary, "read", {"message_id": receipt["message_id"]})["event"] == event
            page = client.call(ordinary, "search", {"query": text, "mode": "exact"})
            assert any(hit["message_id"] == receipt["message_id"] for hit in page["hits"])
            if rounds % 7 == 0:
                operation = {"kind": "source_suppress", "message_id": receipt["message_id"]}
                preview = client.call(owner, "cli", {"command": "manage",
                    "input": {"action": "preview", "operation": operation}})
                client.call(owner, "cli", {"command": "manage", "input": {"action": "confirm",
                    "proposal_id": preview["proposal_id"], "operation": operation}})
                try:
                    client.call(ordinary, "read", {"message_id": receipt["message_id"]})
                except MemoryFailure as error:
                    assert error.code == "message_not_found"
                else:
                    raise AssertionError("suppressed source remained visible")
                suppressed += 1
            if rounds % 11 == 0:
                endpoint = config.parent / "memory-service.json"
                raw = endpoint.read_bytes()
                endpoint.unlink()
                try:
                    offline = subprocess.run([sys.executable, "-B", "-m", "evernook.cli",
                        "remember", "--config", str(config), "--access", paired["access_file"]],
                        input=json.dumps(event).encode(), capture_output=True, timeout=15)
                    assert offline.returncode != 0
                finally:
                    endpoint.write_bytes(raw)
                missing_discovery += 1
            if rounds % 13 == 0:
                stop()
                start()
                assert client.call(owner, "read", {"message_id": first.message_id})["receipt"]["message_id"] == first.message_id
            with progress.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"round": rounds, "elapsed": time.monotonic() - started,
                    "starts": starts, "suppressed": suppressed, "missing_discovery": missing_discovery}) + "\n")
            time.sleep(0.1)
        status = client.call(owner, "status", {})
        scheduler = client.call(owner, "cli", {"command": "scheduler-status", "input": {}})
        drain_deadline = time.monotonic() + 45
        while scheduler["pending"] and time.monotonic() < drain_deadline:
            time.sleep(0.5)
            scheduler = client.call(owner, "cli", {"command": "scheduler-status", "input": {}})
        assert scheduler["pending"] == 0, "maintenance backlog did not converge"
        stop()
        with closing(sqlite3.connect(f"file:{(config.parent / 'memory.sqlite3').as_posix()}?mode=ro", uri=True)) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            count = db.execute("SELECT count(*) FROM messages").fetchone()[0]
            originals = [json.loads(row[0]) for row in db.execute("SELECT event FROM messages WHERE json_extract(event,'$.role')='user'")]
            assert sum(event["source"]["entry"] == "dsh" for event in originals) == rounds
        summary = {"elapsed": time.monotonic() - started, "rounds": rounds, "starts": starts,
            "suppressed": suppressed, "missing_discovery": missing_discovery,
            "messages": count, "service": status["service"], "scheduler": scheduler,
            "quick_check": "ok", "foreign_key_errors": 0, "service_exit": process.poll() if process else None}
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"rounds": rounds, "elapsed": summary["elapsed"], "starts": starts, "passed": True}))
    finally:
        stop()


if __name__ == "__main__":
    main()
