"""Bounded JSON frames and mutual authentication for the local service."""
from __future__ import annotations

import hmac
import json
import secrets
import socket
import time
import threading
from typing import Any

from .models import MemoryFailure

MAX_FRAME = 8 * 1024 * 1024


def _timeout(connection: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MemoryFailure("service_timeout")
    connection.settimeout(remaining)


def _read(connection: socket.socket, size: int, deadline: float,
          cancel: threading.Event | None = None) -> bytes:
    parts: list[bytes] = []
    while size:
        _timeout(connection, deadline)
        if cancel is not None:
            if cancel.is_set():
                raise MemoryFailure("service_canceled")
            connection.settimeout(min(0.1, deadline - time.monotonic()))
        try:
            part = connection.recv(min(size, 65536))
        except TimeoutError:
            if cancel is not None:
                continue
            raise
        if not part:
            raise MemoryFailure("service_disconnected")
        parts.append(part)
        size -= len(part)
    return b"".join(parts)


def _write(connection: socket.socket, value: bytes, deadline: float) -> None:
    _timeout(connection, deadline)
    connection.sendall(value)


def authenticate_client(connection: socket.socket, key: bytes, deadline: float) -> None:
    challenge = secrets.token_bytes(32)
    _write(connection, challenge, deadline)
    reply = _read(connection, 64, deadline)
    expected = hmac.digest(key, b"evernook-server-v1" + challenge + reply[:32], "sha256")
    if not hmac.compare_digest(reply[32:], expected):
        raise MemoryFailure("service_authentication_failed")
    _write(connection, hmac.digest(key, b"evernook-client-v1" + challenge + reply[:32], "sha256"), deadline)


def authenticate_server(connection: socket.socket, key: bytes, deadline: float) -> None:
    challenge = _read(connection, 32, deadline)
    nonce = secrets.token_bytes(32)
    proof = hmac.digest(key, b"evernook-server-v1" + challenge + nonce, "sha256")
    _write(connection, nonce + proof, deadline)
    actual = _read(connection, 32, deadline)
    expected = hmac.digest(key, b"evernook-client-v1" + challenge + nonce, "sha256")
    if not hmac.compare_digest(actual, expected):
        raise MemoryFailure("service_authentication_failed")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError


def receive_json(connection: socket.socket, deadline: float,
                 cancel: threading.Event | None = None) -> dict[str, Any]:
    size = int.from_bytes(_read(connection, 4, deadline, cancel), "big")
    if not 0 < size <= MAX_FRAME:
        raise MemoryFailure("service_frame_too_large")
    try:
        value = json.loads(_read(connection, size, deadline, cancel).decode("utf-8"),
            object_pairs_hook=_unique, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (UnicodeError, ValueError, RecursionError):
        raise MemoryFailure("invalid_service_json") from None


def send_json(connection: socket.socket, value: object, deadline: float) -> None:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise MemoryFailure("service_frame_too_large")
    _write(connection, len(raw).to_bytes(4, "big") + raw, deadline)
