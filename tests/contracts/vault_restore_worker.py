"""Synthetic process-crash injection around copy-based vault staging publication."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook.service import client_access
from evernook.vault_restore import PrivateTree, _copy_between, restore_stage

root, mode = Path(sys.argv[1]), sys.argv[2]
config = root / 'restored' / 'runtime.json'
access = client_access(config, root / 'restored' / 'mcp-access-owner.json')

if mode == 'before':
    def crash_tree(*args: object, **kwargs: object) -> PrivateTree:
        os._exit(91)
        raise AssertionError('unreachable')
    with patch('evernook.vault_restore.PrivateTree', side_effect=crash_tree):
        restore_stage(config, access, root / 'vault-backup', root / 'crash-before',
                      root / 'backup-keys' / 'recovery.json', b'synthetic-backup-password-212',
                      b'synthetic-vault-password-212')
else:
    calls = 0
    def copy_then_crash(source: object, target: object) -> None:
        global calls
        calls += 1
        _copy_between(source, target)  # type: ignore[arg-type]
        if calls == 4:  # inbox.sqlite3 is the last published file.
            os._exit(91)
    with patch('evernook.vault_restore._copy_between', side_effect=copy_then_crash):
        restore_stage(config, access, root / 'vault-backup', root / 'crash-after',
                      root / 'backup-keys' / 'recovery.json', b'synthetic-backup-password-212',
                      b'synthetic-vault-password-212')
