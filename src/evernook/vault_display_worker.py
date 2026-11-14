"""Service-owned local display. Its anonymous pipes are never exposed to a harness."""
from __future__ import annotations

import json
import sys
import tkinter as tk
from types import SimpleNamespace
from typing import Any, Callable, cast

from .models import MemoryFailure
from .vault_local import LocalDisplayRequest, LocalVaultWindow, input_desktop_available
from .worker import protect_parent_lifetime


def _read(maximum: int) -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(maximum + 1)
    if len(raw) > maximum or not raw.endswith(b'\n'):
        raise MemoryFailure('local_display_unavailable')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise MemoryFailure('local_display_unavailable')
    return value


class _WindowRequest:
    def __init__(self, metadata: dict[str, Any]):
        self.access = SimpleNamespace(client_id=metadata['client_id'])
        self.object_id = metadata['object_id']
        self.version = metadata['version']
        self.purpose = metadata['purpose']
        self._used = False

    def _desktop(self) -> bool:
        return input_desktop_available()

    def cancel(self) -> None:
        self._used = True

    def display_once(self, password: bytes, sink: Callable[[str], None]) -> None:
        if self._used:
            raise MemoryFailure('local_display_expired')
        self._used = True
        raw = json.dumps({'password': password.decode('utf-8')}, ensure_ascii=False).encode('utf-8') + b'\n'
        sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()
        response = _read(8 * 1024 * 1024)
        if set(response) != {'text'} or not isinstance(response['text'], str) or not self._desktop():
            raise MemoryFailure('local_display_unavailable')
        sink(response['text'])


def main() -> None:
    protect_parent_lifetime()
    metadata = _read(4096)
    if (set(metadata) != {'client_id', 'object_id', 'version', 'purpose'}
            or not all(isinstance(metadata[key], str) for key in ('client_id', 'object_id', 'purpose'))
            or type(metadata['version']) is not int or not input_desktop_available()):
        raise MemoryFailure('local_display_unavailable')
    root = tk.Tk()
    LocalVaultWindow(root, cast(LocalDisplayRequest, _WindowRequest(metadata)))
    root.attributes('-topmost', True)
    root.lift()
    root.after_idle(root.focus_force)
    root.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Passwords and private originals never appear in diagnostics.
        raise SystemExit(1) from None
