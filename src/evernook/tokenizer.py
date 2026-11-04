"""Explicit offline target-tokenizer counting; no character-count fallback."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
import subprocess
import threading
import time

from .models import MemoryFailure
from .paths import external
from .runtime import _identifier, _json, _load


class LocalTokenCounter:
    def __init__(self, *, node: Path, package_dir: Path, model_id: str, tokenizer_file: Path,
                 config_file: Path, tokenizer_sha256: str, config_sha256: str):
        _identifier(model_id)
        if not node.is_absolute() or not package_dir.is_absolute():
            raise MemoryFailure("invalid_tokenizer_config")
        for value in (tokenizer_sha256, config_sha256):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise MemoryFailure("invalid_tokenizer_config")
        self.node, self.package_dir = node.resolve(), package_dir.resolve()
        self.model_id = model_id
        self.tokenizer_file, self.config_file = external(tokenizer_file), external(config_file)
        self.tokenizer_sha256, self.config_sha256 = tokenizer_sha256, config_sha256
        self.cancel: threading.Event | None = None
        self.counter_id = "local-tokenizer-v1-" + hashlib.sha256(
            _json([model_id, tokenizer_sha256, config_sha256, "tokenizers-js-0.2.0"]).encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> LocalTokenCounter:
        config = _load(external(path))
        strings = {"node", "package_dir", "model_id", "tokenizer_file", "config_file", "tokenizer_sha256", "config_sha256"}
        if (set(config) != strings | {"schema_version"} or type(config["schema_version"]) is not int
                or config["schema_version"] != 1 or any(not isinstance(config[key], str) for key in strings)):
            raise MemoryFailure("invalid_tokenizer_config")
        return cls(node=Path(config["node"]), package_dir=Path(config["package_dir"]), model_id=config["model_id"],
            tokenizer_file=Path(config["tokenizer_file"]), config_file=Path(config["config_file"]),
            tokenizer_sha256=config["tokenizer_sha256"], config_sha256=config["config_sha256"])

    def count(self, text: str) -> int:
        payload = self._request(text)
        try:
            if self.cancel is not None and self.cancel.is_set():
                raise MemoryFailure("context_canceled")
            with subprocess.Popen([str(self.node), str(Path(__file__).with_name("tokenizer_worker.mjs"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW) as process:
                deadline = time.monotonic() + 10
                sent = False
                try:
                    while True:
                        if self.cancel is not None and self.cancel.is_set():
                            raise MemoryFailure("context_canceled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise MemoryFailure("tokenizer_unavailable")
                        try:
                            output, _ = process.communicate(None if sent else payload, timeout=min(0.1, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            sent = True
                    if process.returncode != 0:
                        raise MemoryFailure("tokenizer_unavailable")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            return self._decode(text, output)
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            raise MemoryFailure("tokenizer_unavailable") from None

    def _request(self, text: str, request_id: int | None = None) -> str:
        if not isinstance(text, str) or len(text) > 1048576:
            raise MemoryFailure("invalid_tokenizer_request")
        value: dict[str, object] = {"text": text, "package_dir": str(self.package_dir), "tokenizer_file": str(self.tokenizer_file),
            "config_file": str(self.config_file), "tokenizer_sha256": self.tokenizer_sha256, "config_sha256": self.config_sha256}
        if request_id is not None:
            value["id"] = request_id
        return _json(value)

    @staticmethod
    def _decode(text: str, output: str, request_id: int | None = None) -> int:
        value = json.loads(output)
        expected = {"count"} if request_id is None else {"id", "count"}
        if (not isinstance(value, dict) or set(value) != expected or type(value["count"]) is not int
                or value["count"] < 0 or (bool(text) and value["count"] == 0)
                or (request_id is not None and (type(value["id"]) is not int or value["id"] != request_id))):
            raise MemoryFailure("invalid_token_counter")
        return int(value["count"])

    @contextmanager
    def session(self, *, timeout: float = 10) -> Iterator[_TokenSession]:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise MemoryFailure("invalid_context_request")
        session = _TokenSession(self, timeout)
        try:
            yield session
        finally:
            session.close()


class _TokenSession:
    def __init__(self, counter: LocalTokenCounter, timeout: float):
        self.counter_id = counter.counter_id
        self.counter = counter
        self.deadline = time.monotonic() + timeout
        self.process: subprocess.Popen[str] | None = None
        self.timer: threading.Timer | None = None
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evernook-tokenizer")
        self.lock = threading.Lock()
        self.closed = False
        self.request_id = 0

    def _remaining(self, deadline: float) -> float:
        if self.counter.cancel is not None and self.counter.cancel.is_set():
            raise MemoryFailure("context_canceled")
        remaining = min(self.deadline, deadline) - time.monotonic()
        if remaining <= 0:
            raise MemoryFailure("tokenizer_unavailable")
        return remaining

    @staticmethod
    def _exchange(process: subprocess.Popen[str], payload: str) -> str:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(payload + "\n")
        process.stdin.flush()
        return process.stdout.readline(4096)

    def count(self, text: str) -> int:
        try:
            deadline = time.monotonic() + 10
            self._remaining(deadline)
            with self.lock:
                if self.closed:
                    raise MemoryFailure("tokenizer_unavailable")
                self.request_id += 1
                request_id = self.request_id
                payload = self.counter._request(text, request_id)
                self._remaining(deadline)
                if self.process is None:
                    self.process = subprocess.Popen([str(self.counter.node), str(Path(__file__).with_name("tokenizer_worker.mjs"))],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
                    process = self.process
                    def expire() -> None:
                        if process.poll() is None:
                            try:
                                process.kill()
                            except OSError:
                                pass
                    self.timer = threading.Timer(max(.001, self.deadline - time.monotonic()), expire)
                    self.timer.daemon = True
                    self.timer.start()
                process = self.process
                future = self.io.submit(self._exchange, process, payload)
            while True:
                try:
                    output = future.result(timeout=min(.05, self._remaining(deadline)))
                    break
                except FutureTimeoutError:
                    if future.done():
                        output = future.result()
                        break
            self._remaining(deadline)
            if process.poll() not in (None, 0):
                raise MemoryFailure("tokenizer_unavailable")
            return self.counter._decode(text, output, request_id)
        except MemoryFailure:
            self.close()
            raise
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError, CancelledError):
            self.close()
            raise MemoryFailure("tokenizer_unavailable") from None

    def close(self) -> None:
        with self.lock:
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
        self.io.shutdown(wait=True, cancel_futures=True)
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
