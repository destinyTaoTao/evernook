"""Interrupt after a synthetic encrypted inbox has been staged."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Any, Iterator
from unittest.mock import patch

from evernook import MemoryRuntime
from evernook.backup import _decrypt
from evernook.vault_multi_restore import restore_stage
from evernook.vault_stage_publication import publish_guard

root = Path(sys.argv[1])
config = root / 'recovered' / 'runtime.json'
runtime = MemoryRuntime(config, service_owner=True)
owner = runtime.access_from_file(config.parent / 'mcp-access-owner.json')

def interrupt(source: Path, *args: object, **kwargs: object) -> None:
    _decrypt(source, *args, **kwargs)  # type: ignore[arg-type]
    if source.name.endswith('-inbox.enc'):
        os._exit(91)

@contextmanager
def crash_after_publication(*args: object, **kwargs: object) -> Iterator[Any]:
    with publish_guard(*args, **kwargs) as (stream, publish):  # type: ignore[arg-type]
        def publish_then_crash(destination: Path) -> None:
            publish(destination)
            os._exit(92)
        yield stream, publish_then_crash

if len(sys.argv) > 2 and sys.argv[2] == 'published':
    # Synthetic fixture passwords only; the stage proof is verified before this crash.
    passwords = {sys.argv[3]: b'synthetic multi vault password one',
                 sys.argv[4]: b'synthetic multi vault password two'}
    with patch('evernook.vault_multi_restore.publish_guard', side_effect=crash_after_publication):
        restore_stage(config, owner, root / 'vault-backup', root / 'crash-published', root / 'backup-keys' / 'recovery.json',
                      b'synthetic backup password', lambda _slot, inbox_id: passwords[inbox_id])
else:
    with patch('evernook.vault_multi_restore._decrypt', side_effect=interrupt):
        restore_stage(config, owner, root / 'vault-backup', root / 'crash-stage', root / 'backup-keys' / 'recovery.json',
                      b'synthetic backup password', lambda *_: b'synthetic vault password')
