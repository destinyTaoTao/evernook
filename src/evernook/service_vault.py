"""Service-owned local display broker: no plaintext is returned to callers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from .models import Access, MemoryFailure
from .protected import ProtectedInbox
from .runtime import _unique_object
from .vault_local import LocalDisplayRequest


def show(inbox: ProtectedInbox, access: Access, object_id: str, purpose: str, *,
         gate: threading.BoundedSemaphore, cancel: threading.Event | None) -> dict[str, object]:
    if not gate.acquire(blocking=False):
        raise MemoryFailure('local_display_busy')
    process: subprocess.Popen[bytes] | None = None
    done = threading.Event()
    watcher: threading.Thread | None = None
    request: LocalDisplayRequest | None = None
    deadline = time.monotonic() + 160
    try:
        request = LocalDisplayRequest(inbox, access, object_id, purpose)
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('local_display_canceled')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.vault_display_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, 'EVERNOOK_WORKER_PARENT_PID': str(os.getpid())},
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.stdin is not None and process.stdout is not None
        def monitor() -> None:
            while not done.wait(0.05):
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    if process is not None:
                        try: process.kill()
                        except OSError: pass
                    return
        watcher = threading.Thread(target=monitor, name='evernook-local-display-cancel', daemon=True)
        watcher.start()
        metadata = {'client_id': access.client_id, 'object_id': request.object_id,
                    'version': request.version, 'purpose': request.purpose}
        process.stdin.write(json.dumps(metadata, ensure_ascii=False).encode('utf-8') + b'\n'); process.stdin.flush()
        raw = process.stdout.readline(8193)
        if raw:
            if len(raw) > 8192 or not raw.endswith(b'\n'):
                raise MemoryFailure('local_display_unavailable')
            try:
                supplied = json.loads(raw, object_pairs_hook=_unique_object)
                if not isinstance(supplied, dict) or set(supplied) != {'password'} or not isinstance(supplied['password'], str):
                    raise ValueError()
                password = supplied['password'].encode('utf-8')
            except (ValueError, TypeError, UnicodeError):
                raise MemoryFailure('local_display_unavailable') from None
            handoff: list[str] = []
            def local_sink(text: str) -> None:
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    raise MemoryFailure('local_display_canceled')
                # The authorization transaction hands off in memory only. Pipe I/O
                # must not hold the database write lock if the window stalls.
                handoff.append(text)
            try:
                request.display_once(password, local_sink)
                if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                    raise MemoryFailure('local_display_canceled')
                process.stdin.write(json.dumps({'text': handoff.pop()}, ensure_ascii=False).encode('utf-8') + b'\n')
                process.stdin.flush()
            except MemoryFailure:
                if process.poll() is None:
                    try:
                        process.stdin.write(b'{}\n'); process.stdin.flush()
                    except OSError: pass
            finally:
                password = b''; raw = b''; supplied = {}; handoff.clear()
        remaining = max(0.1, deadline - time.monotonic())
        process.wait(timeout=remaining)
        if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
            raise MemoryFailure('local_display_canceled')
        if process.returncode != 0:
            raise MemoryFailure('local_display_unavailable')
        return {'request_id': request.request_id, 'destination': 'local_window', 'closed': True}
    except MemoryFailure:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise MemoryFailure('local_display_unavailable') from None
    finally:
        done.set()
        if process is not None:
            if process.poll() is None:
                try: process.kill()
                except OSError: pass
            try: process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError): pass
            try:
                if process.stdin: process.stdin.close()
                if process.stdout: process.stdout.close()
            except OSError:
                pass
        if watcher is not None: watcher.join(timeout=1)
        if request is not None: request.cancel()
        gate.release()
