"""Crash a synthetic protected write after staging and before reconciliation."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook import Event, MemoryRuntime, Source
from evernook.protected import ProtectedInbox
from evernook.runtime import _identities, _load

root = Path(sys.argv[1])
config = root / 'restored' / 'runtime.json'
runtime = MemoryRuntime(config, service_owner=True)
access = runtime.access_from_file(config.parent / 'mcp-access-owner.json')
entry = _identities(_load(config.parent / 'client.json'))[access.client_id].entry
inbox = ProtectedInbox(runtime, root / 'staged-vault' / 'inbox', root / 'staged-vault' / 'keys')
event = Event(Source(entry, 'private', 'main', 'crash-pending'), 'user', 4, None,
              'complete', 'synthetic-pending-secret', sensitive=True)
with patch.object(ProtectedInbox, '_reconcile_authorized', side_effect=lambda *_: os._exit(91)):
    inbox.submit(access, event)
