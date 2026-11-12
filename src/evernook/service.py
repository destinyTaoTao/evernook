"""Independent local memory process. No caller-selected paths or executable commands."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import msvcrt
from pathlib import Path
import secrets
import socket
import socketserver
import sys
import threading
import time
from typing import Any

from .models import Access, Event, MemoryFailure, Source
from .paths import external, verify_private_directory
from .runtime import MemoryRuntime, _atomic_json
from .semantic import SemanticRecall
from .service_transport import authenticate_client, authenticate_server, receive_json, send_json

ENDPOINT = "memory-service.json"


class ServiceClient:
    def __init__(self, config: Path):
        self.config = external(config)

    def call(self, access: Access, operation: str, arguments: dict[str, Any],
             *, timeout: float = 30) -> dict[str, Any]:
        if not 0 < timeout <= 300:
            raise MemoryFailure("invalid_service_timeout")
        try:
            verify_private_directory(self.config.parent, extra_files=(self.config.parent / ENDPOINT,))
            endpoint = json.loads((self.config.parent / ENDPOINT).read_text(encoding="utf-8"))
            if (set(endpoint) != {"version", "port", "key"} or endpoint["version"] != 1
                    or type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535):
                raise MemoryFailure("invalid_service_endpoint")
            key = bytes.fromhex(endpoint["key"])
            if len(key) != 32:
                raise MemoryFailure("invalid_service_endpoint")
            deadline = time.monotonic() + timeout
            with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=min(timeout, 5)) as connection:
                authenticate_client(connection, key, deadline)
                send_json(connection, {"version": 1, "access": asdict(access),
                    "operation": operation, "arguments": arguments}, deadline)
                response = receive_json(connection, deadline)
            if response.get("ok") is not True:
                code = response.get("error", {}).get("code", "service_failed")
                raise MemoryFailure(code if isinstance(code, str) and code.isidentifier() else "service_failed")
            result = response["result"]
            if not isinstance(result, dict):
                raise MemoryFailure("invalid_service_response")
            return result
        except MemoryFailure:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise MemoryFailure("service_unavailable") from None


def dispatch(runtime: MemoryRuntime, request: dict[str, Any]) -> object:
    if set(request) != {"version", "access", "operation", "arguments"} or request["version"] != 1:
        raise MemoryFailure("invalid_service_request")
    if not isinstance(request["access"], dict) or not isinstance(request["arguments"], dict):
        raise MemoryFailure("invalid_service_request")
    access = Access(**request["access"])
    runtime.handshake(access)
    arguments = request["arguments"]
    operation = request["operation"]
    if operation == "remember":
        if set(arguments) != {"event"}:
            raise MemoryFailure("invalid_arguments")
        event = dict(arguments["event"])
        source = Source(**event.pop("source"))
        return runtime.submit(access, Event(source=source, **event))
    if operation == "read":
        stored = runtime.read(access, **arguments)
        if stored is None:
            raise MemoryFailure("message_not_found")
        return stored
    if operation == "search":
        return runtime.search(access, **arguments)
    if operation == "recall":
        return SemanticRecall(runtime).recall(access, **arguments)
    if operation == "status" and not arguments:
        return {"capabilities": runtime.handshake(access), "ledger": asdict(runtime.status(access)),
            "lexical": asdict(runtime.lexical_status(access))}
    raise MemoryFailure("unknown_service_operation")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, runtime: MemoryRuntime, key: bytes):
        self.runtime = runtime
        self.key = key
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default traceback can contain private request values.
        pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        deadline = time.monotonic() + 5
        try:
            authenticate_server(self.request, server.key, deadline)
        except (OSError, MemoryFailure):
            return
        try:
            request = receive_json(self.request, deadline)
            result = dispatch(server.runtime, request)
            if is_dataclass(result) and not isinstance(result, type):
                result = asdict(result)
            response: dict[str, object] = {"version": 1, "ok": True, "result": result}
        except MemoryFailure as error:
            response = {"version": 1, "ok": False, "error": {"code": error.code}}
        except (ValueError, TypeError, KeyError):
            response = {"version": 1, "ok": False, "error": {"code": "invalid_arguments"}}
        except Exception:
            response = {"version": 1, "ok": False, "error": {"code": "service_operation_failed"}}
        try:
            send_json(self.request, response, time.monotonic() + 5)
        except (OSError, MemoryFailure):
            pass  # Commit may have completed. Stable source IDs allow receipt recovery.


def serve(config: Path) -> None:
    runtime = MemoryRuntime(config)
    lock_path = runtime.root / "memory-service.lock"
    verify_private_directory(runtime.root, extra_files=(lock_path, runtime.root / ENDPOINT))
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise MemoryFailure("service_already_running") from None
        try:
            key = secrets.token_bytes(32)
            with _Server(runtime, key) as server:
                _atomic_json(runtime.root / ENDPOINT,
                    {"version": 1, "port": server.server_address[1], "key": key.hex()})
                server.serve_forever(poll_interval=0.2)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="EverNook independent loopback service")
    parser.add_argument("--config", type=Path, required=True)
    try:
        serve(parser.parse_args().config)
    except KeyboardInterrupt:
        pass
    except MemoryFailure as error:
        sys.stderr.write(f"evernook:{error.code}\n")
        raise SystemExit(1) from None
    except Exception:
        sys.stderr.write("evernook:service_startup_failed\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
