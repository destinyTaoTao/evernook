"""Bounded, owned offline embedding sessions."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from .models import MemoryFailure

if TYPE_CHECKING:
    from .embedding import LocalMiniLM

_WORKER = Path(__file__).with_name('embedding_worker.mjs')


class EmbeddingSession:
    """One serialized worker, bounded by context lifetime and each request."""

    def __init__(self, backend: LocalMiniLM, timeout: float):
        self.backend, self.space = backend, backend.space
        self.expires_at = time.monotonic() + timeout
        self.closed = False
        self.process: subprocess.Popen[bytes] | None = None
        self.timer: threading.Timer | None = None
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix='evernook-embedding')
        self.request_lock, self.state_lock = threading.Lock(), threading.Lock()
        self.request_id = 0

    @staticmethod
    def _exchange(process: subprocess.Popen[bytes], payload: bytes) -> bytes:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(payload + b'\n')
        process.stdin.flush()
        return process.stdout.readline(1048577)

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        self.backend._validate_request(texts, timeout)
        deadline = min(self.expires_at, time.monotonic() + timeout)

        def remaining() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('embedding_canceled')
            if self.closed:
                raise MemoryFailure('embedding_unavailable')
            value = deadline - time.monotonic()
            if value <= 0:
                raise MemoryFailure('embedding_timeout')
            return value

        # A canceled waiter owns no request and must not kill another caller's
        # in-flight work. Only errors after acquiring this slot close the worker.
        while not self.request_lock.acquire(timeout=min(.05, remaining())):
            pass
        try:
            remaining()
            self.request_id += 1
            payload = self.backend._request(texts, remaining, request_id=self.request_id)
            if len(payload) > 131072:
                raise MemoryFailure('invalid_embedding_request')
            with self.state_lock:
                remaining()
                if self.process is None:
                    environment = {**os.environ, 'EVERNOOK_WORKER_PARENT_PID': str(os.getpid())}
                    self.process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.embedding_session',
                        '--worker', str(self.backend.node), str(_WORKER)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
                    process = self.process
                    def expire() -> None:
                        if process.poll() is None:
                            try:
                                process.kill()
                            except OSError:
                                pass
                    self.timer = threading.Timer(max(.001, self.expires_at - time.monotonic()), expire)
                    self.timer.daemon = True
                    self.timer.start()
                process = self.process
                future = self.io.submit(self._exchange, process, payload)
            while True:
                try:
                    output = future.result(timeout=min(.05, remaining()))
                    break
                except FutureTimeoutError:
                    if future.done():
                        output = future.result()
                        break
            remaining()
            if process.poll() not in (None, 0):
                raise MemoryFailure('embedding_unavailable')
            return self.backend._decode(output, len(texts), request_id=self.request_id)
        except MemoryFailure:
            self.close()
            raise
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError, CancelledError, RuntimeError):
            self.close()
            raise MemoryFailure('embedding_unavailable') from None
        finally:
            self.request_lock.release()

    def close(self) -> None:
        with self.state_lock:
            if self.closed:
                return
            self.closed = True
            if self.timer is not None:
                self.timer.cancel()
            process = self.process
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
        # The guarded wrapper's job is killed when its parent exits, including
        # abrupt owner death; the bounded read therefore also reaches EOF.
        self.io.shutdown(wait=True, cancel_futures=True)
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()


class ManagedEmbedding:
    """Service-owned rotating sessions; each caller keeps its original budget."""

    def __init__(self, backend: LocalMiniLM):
        self.backend = backend
        self.lock = threading.Lock()
        self.session: EmbeddingSession | None = None
        self.closed = False

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        self.backend._validate_request(texts, timeout)
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('embedding_canceled')
            if self.closed:
                raise MemoryFailure('embedding_unavailable')
            value = deadline - time.monotonic()
            if value <= 0:
                raise MemoryFailure('embedding_timeout')
            return value

        while not self.lock.acquire(timeout=min(.05, remaining())):
            pass
        try:
            budget = remaining()
            session = self.session
            if session is None or session.closed or session.expires_at < time.monotonic() + budget:
                if session is not None:
                    session.close()
                session = self.session = EmbeddingSession(self.backend, 120)
            return session.embed(texts, timeout=remaining(), cancel=cancel)
        finally:
            self.lock.release()

    def close(self) -> None:
        self.closed = True
        if self.session is not None:
            self.session.close()


def _worker_main() -> int:
    """Existing Windows job/real-parent-handle guard also owns the Node child."""
    from .worker import protect_parent_lifetime
    protect_parent_lifetime()
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        return 2
    with subprocess.Popen([sys.argv[2], sys.argv[3], '--session'],
            stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW) as process:
        return process.wait()


if __name__ == '__main__':
    raise SystemExit(_worker_main())
