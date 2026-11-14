"""Create an authenticated secure workspace then terminate without cleanup."""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from evernook import MemoryRuntime
from evernook.vault_restore import _stopped_service
from evernook.vault_workspace import secure_workspace

root = Path(sys.argv[1])
runtime = MemoryRuntime(root / 'ordinary' / 'runtime.json', service_owner=True)
with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
    core.execute('BEGIN IMMEDIATE')
    with secure_workspace(runtime, root, 'multi', core=core) as temporary:
        vaults = temporary.mkdir('vaults')
        ledger = temporary.mkdir('ledger')
        for number in ('00', '15'):
            slot = vaults.mkdir(number)
            inbox = slot.mkdir('inbox')
            keys = slot.mkdir('keys')
            db = inbox.create_file('inbox.sqlite3', write_share=True)
            db.stream.close()
            wal = inbox.create_file('inbox.sqlite3-wal', write_share=True)
            shm = inbox.create_file('inbox.sqlite3-shm', write_share=True)
            wal.release()
            shm.release()
            key = keys.create_file('recovery.json')
            key.stream.write(b'synthetic-encrypted-key')
            key.stream.flush()
            key.release()
            with closing(sqlite3.connect(str(inbox.path / 'inbox.sqlite3'))) as check:
                check.execute('PRAGMA journal_mode=WAL')
                check.execute('CREATE TABLE t (x)')
                check.commit()
        held = ledger.create_file('idempotency.key')
        held.stream.write(b'synthetic-crash-key')
        held.stream.flush()
        held.release()
        os._exit(91)
