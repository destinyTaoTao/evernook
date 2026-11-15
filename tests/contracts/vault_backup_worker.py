"""Synthetic crash injection around vault backup publication."""
import os
from pathlib import Path
import sys

from evernook import MemoryRuntime
from evernook.protected import ProtectedInbox
from evernook.vault_backup import create_vault_backup
from evernook.vault_package import publish_package
from evernook.secure_ntfs import PrivateDirectory
from typing import Callable
from unittest.mock import patch

config, inbox_root, vault_keys, backup_keys, destination = map(Path, sys.argv[1:6])
phase = sys.argv[6]
runtime = MemoryRuntime(config)
owner = runtime.access_from_file(config.parent / 'mcp-access-owner.json')
inbox = ProtectedInbox(runtime, inbox_root, vault_keys)
def interrupted(source: PrivateDirectory, target: Path, expected: dict[str, str], check: Callable[[], None]) -> None:
    if phase == 'before':
        os._exit(85)
    publish_package(source, target, expected, check)
    os._exit(86)

with patch('evernook.vault_backup.publish_package', side_effect=interrupted):
    create_vault_backup(runtime, inbox, owner, destination, backup_keys)
