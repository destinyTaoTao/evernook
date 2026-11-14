"""Synthetic process exit immediately after publishing the multi-vault proof."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook import MemoryRuntime
from evernook.protected import ProtectedInbox
from evernook.runtime import _atomic_json
from evernook.vault_multi_proof import prepare

runtime = MemoryRuntime(Path(sys.argv[1]))
access = runtime.access_from_file(Path(sys.argv[2]))
root = Path(sys.argv[3])
first = ProtectedInbox(runtime, root / 'inbox-a', root / 'keys-a')
second = ProtectedInbox(runtime, root / 'inbox-b', root / 'keys-b')

def crash(path: Path, value: object) -> None:
    _atomic_json(path, value)
    os._exit(91)

with patch('evernook.vault_multi_proof._atomic_json', side_effect=crash):
    prepare(runtime, access, ((first, b'synthetic multi vault password one'),
                              (second, b'synthetic multi vault password two')),
            inventory_statement='STOPPED_AND_INVENTORIED')
raise AssertionError('crash point not reached')
