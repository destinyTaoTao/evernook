"""Synthetic crash before or after the attachment transaction commits."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

from evernook.service import client_access
from evernook.vault_attach import attach

root, mode = Path(sys.argv[1]), sys.argv[2]
config = root / 'restored' / 'runtime.json'
access = client_access(config, root / 'restored' / 'mcp-access-owner.json')
def crash(core: sqlite3.Connection) -> None:
    if mode == 'after':
        core.execute('COMMIT')
    os._exit(91)

with patch('evernook.vault_attach._commit_attachment', side_effect=crash):
    attach(config, access, root / 'staged-vault', b'synthetic-vault-password-212')
