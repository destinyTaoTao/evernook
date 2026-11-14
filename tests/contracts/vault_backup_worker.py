"""Synthetic crash injection around vault backup publication."""
import os
from pathlib import Path
import sys

from evernook import MemoryRuntime
from evernook.protected import ProtectedInbox
from evernook.vault_backup import create_vault_backup

config, inbox_root, vault_keys, backup_keys, destination = map(Path, sys.argv[1:6])
phase = sys.argv[6]
runtime = MemoryRuntime(config)
owner = runtime.access_from_file(config.parent / 'mcp-access-owner.json')
inbox = ProtectedInbox(runtime, inbox_root, vault_keys)
original = os.rename

def interrupted(source: object, target: object) -> None:
    if phase == 'before':
        os._exit(85)
    original(source, target)  # type: ignore[arg-type]
    os._exit(86)

os.rename = interrupted  # type: ignore[assignment]
create_vault_backup(runtime, inbox, owner, destination, backup_keys)
