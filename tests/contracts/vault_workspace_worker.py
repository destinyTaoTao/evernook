"""Create an authenticated synthetic workspace then terminate without cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from evernook import MemoryRuntime
from evernook.paths import provision_private_directory
from evernook.vault_restore import _stopped_service
from evernook.vault_workspace import workspace

root = Path(sys.argv[1])
kind = sys.argv[2] if len(sys.argv) > 2 else 'restore'
runtime = MemoryRuntime(root / 'ordinary' / 'runtime.json', service_owner=True)
with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
    core.execute('BEGIN IMMEDIATE')
    with workspace(runtime, root, kind) as temporary:
        ledger = temporary / 'ledger'
        provision_private_directory(ledger)
        (ledger / 'idempotency.key').write_bytes(b'synthetic-crash-key')
        if kind == 'multi':
            vaults = temporary / 'vaults'
            provision_private_directory(vaults)
            for number in ('00', '15'):
                slot = vaults / number
                provision_private_directory(slot)
                for name in ('inbox', 'keys'):
                    provision_private_directory(slot / name)
                (slot / 'inbox' / 'inbox.sqlite3').write_bytes(b'synthetic-encrypted-inbox')
                (slot / 'keys' / 'recovery.json').write_bytes(b'synthetic-encrypted-key')
        os._exit(91)
