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
runtime = MemoryRuntime(root / 'ordinary' / 'runtime.json', service_owner=True)
with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
    core.execute('BEGIN IMMEDIATE')
    with workspace(runtime, root, 'restore') as temporary:
        ledger = temporary / 'ledger'
        provision_private_directory(ledger)
        (ledger / 'idempotency.key').write_bytes(b'synthetic-crash-key')
        os._exit(91)
