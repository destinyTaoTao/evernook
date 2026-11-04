"""Read a synthetic pre-existing profile with a separately installed candidate version."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path

import evernook
from evernook import Event, MemoryRuntime, Source
from evernook.paths import external


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    output = external(args.output)
    if output.exists():
        raise ValueError("new_result_file_required")
    if args.artifact and hashlib.sha256(args.artifact.read_bytes()).hexdigest() != args.expected_sha256:
        output.write_text(json.dumps({"outcome": "refused", "error_code": "artifact_integrity_mismatch", "profile_opened": False}) + "\n", encoding="utf-8")
        return 2
    runtime = MemoryRuntime(args.config)
    actor = runtime.access_from_file(args.access)
    expected = "合成安装原文：37.50元，🙂与e\u0301；保留\r\n换行。"
    event = Event(Source("installed-preflight", "synthetic", "main", "original"), "user", 1, None, "complete", expected)
    source = runtime.read_source(actor, event.source)
    receipt = runtime.submit(actor, event)
    version = importlib.metadata.version("evernook")
    checks = {"installed_version": version == args.expected_version,
              "installed_import": "site-packages" in Path(evernook.__file__).parts,
              "old_original_exact": source is not None and source.event.text == expected,
              "same_source_idempotent": source is not None and receipt == source.receipt,
              "vault_disabled": runtime.handshake(actor)["vault"] is False}
    if args.previous:
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        checks["same_preupgrade_message_id"] = receipt.message_id == previous["message_id"]
    output.write_text(json.dumps({"version": version, "import_file": evernook.__file__, "message_id": receipt.message_id,
        "checks": checks, "scope": "Synthetic existing 0.1.0 data opened by the named installed version; separate environments, no launcher switch, downgrade or user profile activation."},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
