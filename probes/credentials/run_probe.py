"""Run synthetic candidate checks and preserve encrypted-only artifacts outside checkout."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import time

from nacl.public import PrivateKey
from nacl.signing import SigningKey

from vault_probe import BoundObject, Inbox, decrypt, external_directory, seal, unwrap_key, wrap_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output
    external_directory(output.parent)
    if not output.is_absolute():
        raise ValueError("Use an absolute output directory")
    output.mkdir()  # Never overwrite a previous evidence run.
    root = external_directory(output)
    checkout = Path(__file__).resolve().parent
    started = time.monotonic()
    tests = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_vault"], cwd=checkout,
                           env={**os.environ, "EVERNOOK_VAULT_RUN_ROOT": str(root)},
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    (root / "tests.txt").write_text(tests.stdout + tests.stderr, encoding="utf-8")
    private = PrivateKey.generate()
    sender = SigningKey.generate()
    password = secrets.token_bytes(32)  # Test-only secret; never an argument, env var, file or tool parameter.
    sentinel = ("SYNTHETIC_ONLY_" + secrets.token_hex(24)).encode()
    protected_key = wrap_key(private, password)
    (root / "recipient-key.encrypted").write_bytes(protected_key)
    (root / "recipient.public").write_bytes(bytes(private.public_key))
    (root / "sender.public").write_bytes(bytes(sender.verify_key))
    bound = BoundObject("synthetic-original", 1, "synthetic-source", "key-1")
    inbox = Inbox(root, sender.verify_key)
    try:
        envelope = seal(private.public_key, sender, bound, sentinel)
        first = inbox.receive(envelope)
        second = inbox.link(bound)
        inbox.backup(root / "encrypted-objects-backup.sqlite")
        restored = decrypt(unwrap_key((root / "recipient-key.encrypted").read_bytes(), password), sender.verify_key, bound, inbox.read(bound))
        if restored != sentinel:
            raise ValueError("Synthetic encrypted recovery mismatch")
        # Inspect current WAL, database, backup, test output and encrypted key files before close.
        scanned = [path for path in root.rglob("*") if path.is_file()]
        plaintext_files = [str(path.relative_to(root)) for path in scanned if sentinel in path.read_bytes() or password in path.read_bytes() or bytes(private) in path.read_bytes()]
    finally:
        inbox.close()
    manifest = {
        "scope": "T03 synthetic feasibility only; production credential feature remains disabled",
        "python": sys.version, "os": platform.platform(), "durationSeconds": time.monotonic() - started,
        "dependencies": {name: {"version": importlib.metadata.version(name), "license": importlib.metadata.metadata(name).get("License")}
                         for name in ["PyNaCl", "cffi", "mypy"]},
        "sourceHashes": {name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
                         for name in ["vault_probe.py", "test_vault.py", "local_ui.py", "crash_worker.py", "run_probe.py", "requirements.txt"]},
        "receipts": [first, second], "testsExitCode": tests.returncode,
        "scannedFileCount": len(scanned), "plaintextMatches": plaintext_files,
        "recoveredExact": restored == sentinel,
        "limitations": ["GUI controls automated with hidden Tk root; no human/security-desktop acceptance",
                        "single-helper in-process trusted client pairing and approval inputs; no hardened production IPC",
                        "explicit helper lock tested; OS lock-screen events not connected",
                        "Python cannot promise complete secret memory zeroization",
                        "process abrupt exit is not power-loss testing",
                        "no real credentials, detector, production feature or live harness delivery"],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(root), "testsExitCode": tests.returncode, "plaintextMatches": plaintext_files, "recoveredExact": manifest["recoveredExact"]}))
    if tests.returncode or plaintext_files:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
