"""Explicit synthetic local-model integration; no downloader or quality admission."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from evernook import CandidateWorkflow, Event, MemoryRuntime, Source, initialize, pair_client
from evernook.paths import external, provision_private_directory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--transformers-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    if root.exists():
        raise ValueError("Use a new external synthetic directory")
    provision_private_directory(root)
    config, access = initialize(root / "ordinary", client_id="synthetic-extraction", entry="synthetic-extraction")
    access_file = config.parent / "mcp-access.json"
    pair_client(config, client_id=access.client_id, entry="synthetic-extraction", access_path=access_file)
    runtime = MemoryRuntime(config)
    text = "合成：小林喜欢乌龙茶。"
    receipt = runtime.submit(access, Event(Source("synthetic-extraction", "session", "main", "1"), "user", 1, None, "complete", text))
    profile = root / "extraction.json"
    profile.write_text(json.dumps({"schema_version": 1, "profile": "qwen3-0.6b-experimental-extractive-v1",
        "node": str(args.node), "transformers_dir": str(args.transformers_dir), "model_dir": str(args.model_dir)}), encoding="utf-8")
    started = time.monotonic()
    process = subprocess.run([sys.executable, "-m", "evernook.cli", "extract", "--config", str(config), "--access", str(access_file),
        "--extraction-config", str(profile)], input=json.dumps({"source_ids": [receipt.message_id], "timeout": 120}).encode(),
        capture_output=True, timeout=150)
    payload = json.loads(process.stdout)
    original = runtime.read(access, receipt.message_id)
    assert original is not None
    report = {"cli_exit": process.returncode, "seconds": time.monotonic() - started, "response": payload,
        "model_quality_admitted": False, "system_network_disabled": False, "synthetic_only": True,
        "original_unchanged": original.event.text == text}
    if payload.get("ok"):
        report["candidates"] = [asdict(item) for item in CandidateWorkflow(runtime).candidates(access, payload["result"]["run"]["run_id"])]
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(root), "cli_exit": process.returncode, "response": payload, "seconds": report["seconds"]}))


if __name__ == "__main__":
    main()
