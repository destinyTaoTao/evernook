"""Hard-exit destruction at publication boundaries, using synthetic fixtures only."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import patch

from evernook import MemoryRuntime
from evernook import vault_destroy
from evernook.runtime import _atomic_json as original_json
from evernook.vault_workspace import _save as original_workspaces


root = Path(sys.argv[1])
boundary = sys.argv[2]
config = root / 'ordinary' / 'runtime.json'
runtime = MemoryRuntime(config, service_owner=True)
access = runtime.access_from_file(config.parent / 'mcp-access-owner.json')
original_release = vault_destroy._release_binding
original_save = vault_destroy._save_record


def publish(path: Path, value: object) -> None:
    original_json(path, value)
    if path.name == boundary:
        os._exit(93)


def release(*args: object, **kwargs: object) -> None:
    original_release(*args, **kwargs)  # type: ignore[arg-type]
    if boundary == 'before_commit':
        os._exit(93)


def save(memory: MemoryRuntime, content: dict[str, object]) -> None:
    steps = content['steps']
    if boundary == 'after_commit' and isinstance(steps, dict) and steps['release_binding'] == 'done':
        os._exit(93)
    original_save(memory, content)


def save_workspaces(memory: MemoryRuntime, entries: list[dict[str, object]]) -> None:
    original_workspaces(memory, entries)
    if boundary == 'workspace_empty' and not entries:
        os._exit(93)


with patch.object(vault_destroy, '_atomic_json', side_effect=publish), \
        patch.object(vault_destroy, '_release_binding', side_effect=release), \
        patch.object(vault_destroy, '_save_record', side_effect=save), \
        patch.object(vault_destroy, '_save_workspaces', side_effect=save_workspaces):
    vault_destroy.destroy_vault(config, access, root / 'inbox-a', root / 'keys-a',
                              b'synthetic multi vault password one')
raise SystemExit('crash boundary was not reached')
