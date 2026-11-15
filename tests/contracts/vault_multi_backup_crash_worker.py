"""Interrupt synthetic multi-backup after the first staged ciphertext write."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import BinaryIO
from unittest.mock import patch

from evernook import MemoryRuntime
from evernook.protected import ProtectedInbox
from evernook.vault_multi_backup import create_multi_backup

root = Path(sys.argv[1])
runtime = MemoryRuntime(root / 'ordinary' / 'runtime.json', service_owner=True)
owner = runtime.access_from_file(root / 'ordinary' / 'mcp-access-owner.json')
primary = ProtectedInbox(runtime, root / 'inbox-a', root / 'keys-a')
history = ProtectedInbox(runtime, root / 'inbox-b', root / 'keys-b')

def crash(_source: object, destination: BinaryIO, *_args: object, **_kwargs: object) -> object:
    destination.write(b'synthetic-incomplete-ciphertext')
    destination.flush()
    os._exit(91)

with patch('evernook.vault_multi_backup._encrypt_stream', side_effect=crash):
    create_multi_backup(runtime, primary, (primary, history), owner, root / 'crash-backup', root / 'backup-keys')
