"""Synthetic process-crash injection immediately around vault staging publication."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook.service import client_access
from evernook.vault_restore import restore_stage

root, mode = Path(sys.argv[1]), sys.argv[2]
config = root / 'restored' / 'runtime.json'
access = client_access(config, root / 'restored' / 'mcp-access-owner.json')
rename = os.rename
def crash(source: object, destination: object) -> None:
    if mode == 'after':
        rename(source, destination)  # type: ignore[arg-type]
    os._exit(91)

with patch('evernook.vault_restore.os.rename', side_effect=crash):
    restore_stage(config, access, root / 'vault-backup', root / ('crash-' + mode),
                  root / 'backup-keys' / 'recovery.json', b'synthetic-backup-password-212',
                  b'synthetic-vault-password-212')
