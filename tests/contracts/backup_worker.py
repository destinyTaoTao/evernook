"""Synthetic process-exit fixture; secrets are read only from private local files."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook import Access, MemoryRuntime
from evernook.backup import create_backup


def main() -> None:
    config, keys, target = map(Path, sys.argv[1:4])
    identity = json.loads((config.parent / "client.json").read_text(encoding="utf-8"))
    access = Access(identity["client_id"], identity["token"])
    runtime = MemoryRuntime(config)
    original = os.rename

    def rename(source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
               destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        # create_backup resolves the destination (8.3 short names differ from the test path), so compare resolved.
        if Path(destination).resolve() == target.resolve() and sys.argv[4] == "before":
            os._exit(86)
        original(source, destination)

    with patch("os.rename", rename):
        status = create_backup(runtime, access, target, keys)
    print(json.dumps({"backup_id": status.backup_id}), flush=True)
    os._exit(87)


if __name__ == "__main__":
    main()
