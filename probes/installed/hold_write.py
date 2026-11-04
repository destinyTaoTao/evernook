"""Hold a real SQLite writer lock in an explicitly synthetic installed fixture."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

from evernook.paths import external

root = external(Path(sys.argv[1]))
fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
if (not fixture["scope"].startswith("Synthetic public installed-wheel fixture")
        or Path(fixture["config"]) != root / "runtime/runtime.json"):
    raise ValueError("synthetic_fixture_required")
connection = sqlite3.connect(root / "runtime/memory.sqlite3", timeout=5, isolation_level=None)
try:
    connection.execute("BEGIN EXCLUSIVE")
    print("synthetic-writer-locked", flush=True)
    sys.stdin.buffer.read()
finally:
    connection.rollback()
    connection.close()
