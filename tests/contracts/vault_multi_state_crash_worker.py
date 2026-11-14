"""Crash around a synthetic recovered-multi marker transaction."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import sys

from evernook import MemoryRuntime
from evernook.missing_vault import _receipt, read_state
from evernook.runtime import _json
from evernook.vault_multi_binding import read
from evernook.vault_restore import _stopped_service

config = Path(sys.argv[1])
phase = sys.argv[2]
runtime = MemoryRuntime(config, service_owner=True)
with _stopped_service(runtime.root), runtime._connection(ownership_exclusive=True) as core:
    core.execute('BEGIN IMMEDIATE')
    assert read_state(runtime, core)
    content = {'version': 1, 'receipt_auth': _receipt(runtime), 'binding': read(runtime)}
    authentication = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-attachment-v1\0'
                                 + _json(content).encode(), hashlib.sha256).hex()
    core.execute('CREATE TABLE restored_multi_attachment_v1 (id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL, authentication TEXT NOT NULL)')
    core.execute('INSERT INTO restored_multi_attachment_v1 VALUES (1,?,?)', (_json(content), authentication))
    core.execute('DROP TABLE restored_missing_vault_v1')
    if phase == 'before':
        os._exit(91)
    core.execute('COMMIT')
    os._exit(92)
