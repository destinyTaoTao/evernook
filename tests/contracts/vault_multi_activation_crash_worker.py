"""Synthetic crash during the two-file multi-vault activation transition."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook.runtime import _atomic_json
from evernook.service import serve

config, owner, root, phase = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]

def crash(path: Path, value: object) -> None:
    _atomic_json(path, value)
    if (phase == 'binding' and path.name == 'protected-multi-binding.json') or (phase == 'mode' and path.name == 'runtime.json'):
        os._exit(91)

with patch('evernook.vault_multi_binding._atomic_json', side_effect=crash):
    serve(config, activate_owner=owner, activate_verified_multi=True,
          protected_root=root / 'inbox-a', protected_keys=root / 'keys-a',
          protected_history_roots=(root / 'inbox-b',), protected_history_keys=(root / 'keys-b',))
raise AssertionError('crash point not reached')
