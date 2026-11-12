"""Independent local memory process. No caller-selected paths or executable commands."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import hashlib
import msvcrt
from pathlib import Path
import secrets
import select
import socket
import socketserver
import sys
import threading
import time
from typing import Any

from .models import Access, Event, MemoryFailure, Source
from .paths import external, verify_private_directory
from .runtime import MemoryRuntime, _atomic_json, _access_record, _load, _identifier, pair_client
from .semantic import SemanticRecall
from .service_transport import authenticate_client, authenticate_server, receive_json, send_json

ENDPOINT = "memory-service.json"
CLI_OPERATIONS = frozenset({"status", "manage", "host-recovery", "mark-important", "change",
    "purge-preview", "purge", "profile-read", "profile-summary", "profile-decide", "candidate-list", "fact-query",
    "fact-decide", "scheduler-trigger", "scheduler-status", "scheduler-session-end",
    "scheduler-pause", "scheduler-configure", "remember", "search", "recall", "read", "state",
    "preference-update-status", "preference-update-configure", "preference-update-process", "preference-clarify", "preference-resolve",
    "episode-status", "episode-configure", "episode-propose", "episode-read"})


def client_access(config: Path, access_path: Path) -> Access:
    """Load only owner-ACL-checked connection materials; the service authenticates Access."""
    config, access_path = external(config), external(access_path)
    if access_path.parent != config.parent:
        raise MemoryFailure("invalid_access_location")
    verify_private_directory(config.parent, extra_files=(config, access_path))
    value = _load(config)
    if value.get("data_dir") != str(config.parent):
        raise MemoryFailure("invalid_config_location")
    return _access_record(_load(access_path))


class ServiceClient:
    def __init__(self, config: Path):
        self.config = external(config)

    def call(self, access: Access, operation: str, arguments: dict[str, Any],
             *, timeout: float = 30, cancel: threading.Event | None = None) -> Any:
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
                response = receive_json(connection, deadline, cancel)
            if response.get("ok") is not True:
                code = response.get("error", {}).get("code", "service_failed")
                raise MemoryFailure(code if isinstance(code, str) and code.isidentifier() else "service_failed")
            result = response["result"]
            return result
        except MemoryFailure:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise MemoryFailure("service_unavailable") from None


def dispatch(runtime: MemoryRuntime, request: dict[str, Any], *, cancel: threading.Event | None = None,
             context_config: Path | None = None, embedding_config: Path | None = None) -> object:
    if set(request) != {"version", "access", "operation", "arguments"} or request["version"] != 1:
        raise MemoryFailure("invalid_service_request")
    if not isinstance(request["access"], dict) or not isinstance(request["arguments"], dict):
        raise MemoryFailure("invalid_service_request")
    access = Access(**request["access"])
    runtime.handshake(access)
    arguments = request["arguments"]
    operation = request["operation"]
    if operation in ("pair-unlock", "pair-configure"):
        from .runtime import unlock_pairing, configure_lockout
        runtime._require_scope(access, "admin", "service_pair_control")
        if operation == "pair-unlock" and not arguments:
            return unlock_pairing(runtime.root / "runtime.json", service_owner=True)
        if operation == "pair-configure" and set(arguments) == {"failure_limit"}:
            return configure_lockout(runtime.root / "runtime.json",
                failure_limit=arguments["failure_limit"], service_owner=True)
        raise MemoryFailure("invalid_arguments")
    if operation == "pair-client":
        runtime._require_scope(access, "admin", "service_pair")
        if set(arguments) != {"client_id", "entry", "scopes"}:
            raise MemoryFailure("invalid_arguments")
        _identifier(arguments["client_id"])
        _identifier(arguments["entry"])
        scopes = arguments["scopes"]
        if scopes is not None and not isinstance(scopes, list):
            raise MemoryFailure("invalid_scopes")
        destination = runtime.root / ("mcp-access-" + hashlib.sha256(
            arguments["client_id"].encode("utf-8")).hexdigest() + ".json")
        paired = pair_client(runtime.root / "runtime.json", client_id=arguments["client_id"],
            entry=arguments["entry"], access_path=destination,
            scopes=tuple(scopes) if scopes is not None else ("capture", "recall"), service_owner=True)
        return {"client_id": paired.client_id, "access_file": str(destination)}
    if operation == "policy-match":
        expected_policy = {"embedding_config": str(embedding_config.resolve()) if embedding_config else None,
            "context_config": str(context_config.resolve()) if context_config else None}
        if arguments != expected_policy:
            raise MemoryFailure("service_policy_mismatch")
        return {}
    if operation in ("mcp", "mcp-list"):
        from .mcp_server import build_operations
        from .embedding import LocalMiniLM
        expected = {"profile"} if operation == "mcp-list" else {"name", "arguments", "profile"}
        if set(arguments) != expected:
            raise MemoryFailure("invalid_arguments")
        backend = LocalMiniLM.from_file(embedding_config) if embedding_config is not None else None
        operations, context, restricted = build_operations(runtime, access, profile=arguments["profile"],
            backend=backend, context_config=context_config)
        if operation == "mcp-list":
            return {"tools": [{"name": name, "schema": model.model_json_schema(), "description": description}
                for name, (model, _, description) in operations.items()]}
        name = arguments["name"]
        if name not in operations:
            raise MemoryFailure("operation_not_allowed" if name in restricted else "unknown_tool")
        model, invoke, _ = operations[name]
        parsed = model.model_validate_json(json.dumps(arguments["arguments"], ensure_ascii=False))
        if name == "evernook_v1_context" and context is not None:
            return context.build(access, **parsed.model_dump(), cancel=cancel)
        if name == "evernook_v1_recall":
            return SemanticRecall(runtime, backend).recall(access, **parsed.model_dump(), cancel=cancel)
        return invoke(parsed)
    if operation == "cli":
        from .cli import _execute, _parser
        if (set(arguments) != {"command", "input"} or arguments["command"] not in CLI_OPERATIONS
                or not isinstance(arguments["input"], dict)):
            raise MemoryFailure("operation_not_allowed")
        command = arguments["command"]
        parser_arguments = [command, "--config", str(runtime.root / "runtime.json"),
            "--access", str(runtime.root / "unused-service-access.json")]
        if command in ("read", "state"):
            if set(arguments["input"]) != {"message_id"} or not isinstance(arguments["input"]["message_id"], str):
                raise MemoryFailure("invalid_arguments")
            parser_arguments += ["--message-id", arguments["input"]["message_id"]]
        namespace = _parser().parse_args(parser_arguments)
        if command == "recall":
            namespace.embedding_config = embedding_config
        return _execute(namespace, input_value=arguments["input"],
            service_runtime=runtime, service_access=access, service_cancel=cancel)
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
        from .embedding import LocalMiniLM
        backend = LocalMiniLM.from_file(embedding_config) if embedding_config is not None else None
        return SemanticRecall(runtime, backend).recall(access, **arguments, cancel=cancel)
    if operation == "status" and not arguments:
        return {"capabilities": runtime.handshake(access), "ledger": asdict(runtime.status(access)),
            "lexical": asdict(runtime.lexical_status(access))}
    raise MemoryFailure("unknown_service_operation")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, runtime: MemoryRuntime, key: bytes, *,
                 context_config: Path | None = None, embedding_config: Path | None = None):
        self.runtime = runtime
        self.key = key
        self.context_config = context_config
        self.embedding_config = embedding_config
        self.slots = threading.BoundedSemaphore(8)
        self.stopping = threading.Event()
        from .service_maintenance import ServiceMaintenance
        self.maintenance: ServiceMaintenance | None = None
        super().__init__(("127.0.0.1", 0), _Handler)

    def maintenance_status(self) -> dict[str, Any]:
        return self.maintenance.snapshot() if self.maintenance else {"enabled": False}

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
            cancel = threading.Event()
            done = threading.Event()
            def monitor() -> None:
                while not done.wait(0.05):
                    if server.stopping.is_set():
                        cancel.set()
                        return
                    try:
                        readable, _, _ = select.select([self.request], [], [], 0)
                        if readable and not self.request.recv(1, socket.MSG_PEEK):
                            cancel.set()
                            return
                    except OSError:
                        cancel.set()
                        return
            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            try:
                result = dispatch(server.runtime, request, cancel=cancel,
                    context_config=server.context_config, embedding_config=server.embedding_config)
            finally:
                done.set()
                watcher.join()
            is_status = request.get("operation") == "status" or (
                request.get("operation") == "mcp" and request.get("arguments", {}).get("name") == "evernook_v1_status")
            if is_status and isinstance(result, dict):
                result["service"] = {"independent": True, "maintenance": server.maintenance_status()}
                if isinstance(result.get("processing"), dict):
                    result["processing"]["consumer_running"] = server.maintenance is not None
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


def serve(config: Path, *, context_config: Path | None = None, embedding_config: Path | None = None,
          maintenance_access: Path | None = None, maintenance_interval: float = 5,
          navigation_policies: tuple[str, ...] = (), activate_owner: Path | None = None) -> None:
    runtime = MemoryRuntime(config, service_owner=True)
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
            with _Server(runtime, key, context_config=context_config, embedding_config=embedding_config) as server:
                if maintenance_access is not None:
                    from .service_maintenance import ServiceMaintenance
                    server.maintenance = ServiceMaintenance(runtime, maintenance_access, maintenance_interval,
                        navigation_policies, server.stopping, embedding_config=embedding_config)
                if activate_owner is not None:
                    owner = runtime.access_from_file(activate_owner)
                    runtime._require_scope(owner, "admin", "activate_service")
                    with runtime._connection(ownership_exclusive=True) as db:
                        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='protected_links_v1'").fetchone()
                                and db.execute("SELECT 1 FROM protected_links_v1 LIMIT 1").fetchone()):
                            raise MemoryFailure("protected_cutover_requires_migration")
                        db.execute("BEGIN EXCLUSIVE")
                        value = _load(config)
                        _atomic_json(config, {**value, "service_mode": True})
                        db.execute("COMMIT")
                _atomic_json(runtime.root / ENDPOINT,
                    {"version": 1, "port": server.server_address[1], "key": key.hex()})
                if server.maintenance:
                    server.maintenance.thread.start()
                try:
                    server.serve_forever(poll_interval=0.2)
                finally:
                    server.stopping.set()
                    if server.maintenance:
                        server.maintenance.thread.join()
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="EverNook independent loopback service")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--context-config", type=Path)
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--maintenance-access", type=Path)
    parser.add_argument("--maintenance-interval", type=float, default=5)
    parser.add_argument("--navigation-policy", action="append", default=[])
    parser.add_argument("--activate-owner", type=Path)
    try:
        args = parser.parse_args()
        serve(args.config, context_config=args.context_config, embedding_config=args.embedding_config,
            maintenance_access=args.maintenance_access, maintenance_interval=args.maintenance_interval,
            navigation_policies=tuple(args.navigation_policy), activate_owner=args.activate_owner)
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
