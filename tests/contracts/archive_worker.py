"""Crash the archive publication boundary using synthetic external runtime data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook import Access, MemoryRuntime, restore_archive


def main() -> None:
    operation, source, target = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

    def before_publish(old: Path, new: Path) -> None:
        os._exit(86)

    with patch("os.rename", before_publish):
        if operation == "restore":
            restore_archive(source, target)
        else:
            client = json.loads((source.parent / "client.json").read_text(encoding="utf-8"))
            MemoryRuntime(source).export_archive(Access(client["client_id"], client["token"]), target)
    raise AssertionError("publication crash point not reached")


if __name__ == "__main__":
    main()
