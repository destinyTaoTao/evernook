"""Independent local memory process. No caller-selected paths or executable commands."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import json
import hashlib
import hmac
import msvcrt
from pathlib import Path
import secrets
import select
import socket
import socketserver
import sys
import threading
import time
from typing import Any, TYPE_CHECKING

from .models import Access, Event, MemoryFailure, Source
from .embedding import ServiceEmbedding
from .paths import external, verify_private_directory
from .runtime import MemoryRuntime, _atomic_json, _access_record, _load, _identifier, _json, pair_client
from .semantic import SemanticRecall
from .reflection import ReflectionEngine
from .service_transport import (authenticate_client, authenticate_server, open_private_frame,
                                receive_json, seal_private_frame, send_json, serial_operation,
                                OPEN_SERIAL, SERIAL_READY, SINGLE_READY, SERIAL_IDLE)

if TYPE_CHECKING:
    from .extraction import ExtractorBackend
    from .protected import ProtectedInbox

ENDPOINT = "memory-service.json"
PRIVATE_OPERATIONS = frozenset({'protected-receive', 'protected-inboxes', 'protected-inventory', 'protected-links', 'protected-show-local', 'protected-backup', 'transcription-submit', 'transcription-read'})
CLI_OPERATIONS = frozenset({"status", "manage", "host-recovery", "mark-important", "change",
    "purge-preview", "purge", "profile-read", "profile-summary", "profile-history", "profile-decide", "candidate-list", "fact-query",
    "experience-queue", "experience-read", "experience-decide",
    "skill-draft-configure", "skill-draft-status",
    "persona-read", "persona-history", "persona-set", "persona-rollback",
    "fact-decide", "scheduler-trigger", "scheduler-status", "scheduler-session-end",
    "scheduler-pause", "scheduler-configure", "remember", "search", "recall", "read", "state",
    "preference-update-status", "preference-update-configure", "preference-update-process", "preference-clarify", "preference-resolve",
    "episode-status", "episode-configure", "episode-propose", "episode-read",
    "observation-propose", "observation-rebind", "observation-read", "observation-decide", "observation-split", "observation-related",
    "observation-query", "observation-configure", "observation-status", "observation-process", "observation-evidence",
    "topic-view-define", "topic-view-read", "topic-view-control", "topic-view-status", "topic-view-refresh", "topic-view-tick", "topic-view-edit", "reflect", "reflect-configure", "context", "extract", "extract-preferences"})


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
    def submit_transcription(self, access: Access, material: object, *, timeout: float = 30,
                             cancel: threading.Event | None = None) -> dict[str, Any]:
        from .transcription import Transcription
        if not isinstance(material, Transcription):
            raise MemoryFailure('invalid_transcription')
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('service_canceled')
        value = self.call(access, 'transcription-submit', {'material': asdict(material)}, timeout=timeout, cancel=cancel)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def read_transcription(self, access: Access, message_id: str, *, timeout: float = 30,
                           cancel: threading.Event | None = None) -> dict[str, Any] | None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('service_canceled')
        value = self.call(access, 'transcription-read', {'message_id': message_id}, timeout=timeout, cancel=cancel)
        if value is not None and not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_receive(self, access: Access, event: Event, *, timeout: float = 30) -> dict[str, Any]:
        value = self.call(access, 'protected-receive', {'event': asdict(event)}, timeout=timeout)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_capture(self, access: Access, request: object, *, timeout: float = 30,
                          cancel: threading.Event | None = None) -> dict[str, Any]:
        from .models import Capture
        from .private_capture_receipt import capture
        if not isinstance(request, Capture):
            raise MemoryFailure('invalid_private_capture')
        return capture(self, access, request, timeout=timeout, cancel=cancel)

    def protected_inventory(self, access: Access, *, after: str | None = None, limit: int = 100,
                            inbox_id: str | None = None) -> dict[str, Any]:
        value = self.call(access, 'protected-inventory', {'after': after, 'limit': limit, **({'inbox_id': inbox_id} if inbox_id is not None else {})})
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_inboxes(self, access: Access) -> dict[str, Any]:
        value = self.call(access, 'protected-inboxes', {})
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_backup(self, access: Access, destination: Path, backup_keys: Path, *, timeout: float = 120,
                         cancel: threading.Event | None = None) -> dict[str, Any]:
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise MemoryFailure('invalid_vault_backup_request')
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('service_canceled')
        value = self.call(access, 'protected-backup', {'destination': str(destination), 'backup_keys': str(backup_keys),
                          'timeout': timeout}, timeout=timeout + 10, cancel=cancel)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_links(self, access: Access, *, after: str | None = None, limit: int = 100,
                        inbox_id: str | None = None) -> dict[str, Any]:
        value = self.call(access, 'protected-links', {'after': after, 'limit': limit, **({'inbox_id': inbox_id} if inbox_id is not None else {})})
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def protected_show_local(self, access: Access, object_id: str, purpose: str, *,
                             cancel: threading.Event | None = None, inbox_id: str | None = None) -> dict[str, Any]:
        value = self.call(access, 'protected-show-local', {'object_id': object_id, 'purpose': purpose,
                          **({'inbox_id': inbox_id} if inbox_id is not None else {})}, timeout=170, cancel=cancel)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def context(self, access: Access, query: str, *, model_id: str, max_tokens: int, as_of: str,
                mode: str = 'normal', timeout: float = 30, current_input_id: str | None = None,
                query_intent: str | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
        value = self.call(access, 'mcp', {'name': 'evernook_v1_context', 'profile': 'trusted-host',
            'arguments': {'query': query, 'model_id': model_id, 'max_tokens': max_tokens, 'as_of': as_of,
                'mode': mode, 'timeout': timeout, 'current_input_id': current_input_id,
                'query_intent': query_intent}}, timeout=130, cancel=cancel)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def reflect(self, access: Access, query: str, *, mode: str = 'deep', timeout: float = 60,
                topic_view_ids: tuple[str, ...] = (), current_input_id: str | None = None,
                allow_remote: bool = False, include_control_history: bool = False,
                cancel: threading.Event | None = None) -> dict[str, Any]:
        if type(allow_remote) is not bool or type(include_control_history) is not bool:
            raise MemoryFailure('invalid_reflection_request')
        value = self.call(access, 'cli', {'command': 'reflect', 'input': {'query': query, 'mode': mode,
            'timeout': timeout, 'topic_view_ids': list(topic_view_ids), 'current_input_id': current_input_id,
            **({'allow_remote': True} if allow_remote else {}),
            **({'include_control_history': True} if include_control_history else {})}}, timeout=130, cancel=cancel)
        if not isinstance(value, dict):
            raise MemoryFailure('service_invalid_response')
        return value

    def __init__(self, config: Path, *, reuse: bool = False):
        self.config = external(config)
        self._reuse = reuse
        self._lock = threading.Lock()
        self._idle: tuple[tuple[int, bytes], socket.socket, bytes, float] | None = None
        self._legacy: tuple[int, bytes] | None = None
        self._closed = False

    def close(self) -> None:
        """Release the idle connection. In-flight calls finish without returning a socket."""
        with self._lock:
            self._closed = True
            if self._idle is not None:
                self._idle[1].close()
                self._idle = None

    @contextmanager
    def _connection(self, port: int, key: bytes, deadline: float, cancel: threading.Event | None,
                    *, serial: bool) -> Iterator[tuple[socket.socket, bytes]]:
        identity = (port, key)
        connection: socket.socket | None = None
        session = b""
        reusable = False
        with self._lock:
            if self._closed:
                raise MemoryFailure("service_client_closed")
            negotiate = serial and self._legacy != identity
            if negotiate and self._idle is not None:
                previous, cached, session, expires = self._idle
                self._idle = None
                if previous == identity and time.monotonic() < expires and not select.select([cached], [], [], 0)[0]:
                    connection, reusable = cached, True
                else:
                    cached.close()
        try:
            while connection is None:
                if cancel is not None and cancel.is_set():
                    raise MemoryFailure("service_canceled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MemoryFailure("service_timeout")
                connection = socket.create_connection(("127.0.0.1", port), timeout=min(remaining, 5))
                session = authenticate_client(connection, key, deadline, cancel=cancel)
                if not negotiate:
                    break
                send_json(connection, OPEN_SERIAL, deadline, cancel=cancel)
                reply = receive_json(connection, deadline, cancel)
                if reply == SERIAL_READY:
                    reusable = True
                elif reply == {"version": 1, "ok": False, "error": {"code": "invalid_service_request"}}:
                    # An authenticated v1 server rejected only the control frame.
                    with self._lock:
                        self._legacy = identity
                    connection.close()
                    connection = None
                    negotiate = False
                elif reply != SINGLE_READY:
                    raise MemoryFailure("invalid_service_response")
            yield connection, session
            if reusable:
                with self._lock:
                    if not self._closed and self._idle is None:
                        self._idle = (identity, connection, session, time.monotonic() + SERIAL_IDLE / 2)
                        connection = None
        finally:
            if connection is not None:
                connection.close()

    def call(self, access: Access, operation: str, arguments: dict[str, Any],
             *, timeout: float = 30, cancel: threading.Event | None = None) -> Any:
        if not 0 < timeout <= 300:
            raise MemoryFailure("invalid_service_timeout")
        if cancel is not None and cancel.is_set():
            raise MemoryFailure("service_canceled")
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
            with self._connection(endpoint["port"], key, deadline, cancel,
                                  serial=self._reuse and serial_operation(operation, arguments)) as (connection, session):
                request = {"version": 1, "access": asdict(access), "operation": operation, "arguments": arguments}
                private = operation in PRIVATE_OPERATIONS
                frame = seal_private_frame(session, request, direction='request') if private else request
                if cancel is not None and cancel.is_set():
                    raise MemoryFailure("service_canceled")
                send_json(connection, frame, deadline, cancel=cancel)
                response = receive_json(connection, deadline, cancel)
                if private:
                    if 'sealed' not in response:
                        raise MemoryFailure('protected_encryption_required')
                    response = open_private_frame(session, response, direction='response')
            if response.get("ok") is not True:
                code = response.get("error", {}).get("code", "service_failed")
                raise MemoryFailure(code if isinstance(code, str) and code.isidentifier() else "service_failed")
            result = response["result"]
            return result
        except MemoryFailure:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise MemoryFailure("service_unavailable") from None


class _ProviderBinding:
    """The exact parsed configuration used to construct a service provider."""
    def __init__(self, path: Path, kind: str):
        self.requested_path = path
        self.path = external(path)
        self.kind = kind
        self.config = _load(self.path)
        self.identity = _json(self.config)

    def check(self) -> None:
        try:
            if external(self.requested_path) != self.path:
                raise MemoryFailure(self.kind + '_config_changed')
            current = _json(_load(self.path))
        except MemoryFailure:
            raise MemoryFailure(self.kind + '_config_changed') from None
        if current != self.identity:
            raise MemoryFailure(self.kind + '_config_changed')


def dispatch(runtime: MemoryRuntime, request: dict[str, Any], *, cancel: threading.Event | None = None,
             context_config: Path | None = None, embedding_config: Path | None = None, reflector: ReflectionEngine | None = None,
             protected_inbox: ProtectedInbox | None = None, display_gate: threading.BoundedSemaphore | None = None,
             extractor: ExtractorBackend | None = None, extraction_gate: threading.BoundedSemaphore | None = None,
             embedding_binding: ServiceEmbedding | None = None, extraction_error: str | None = None,
             reflection_error: str | None = None, extraction_binding: _ProviderBinding | None = None,
             reflection_binding: _ProviderBinding | None = None,
             protected_history: dict[str, ProtectedInbox] | None = None) -> object:
    if set(request) != {"version", "access", "operation", "arguments"} or request["version"] != 1:
        raise MemoryFailure("invalid_service_request")
    if not isinstance(request["access"], dict) or not isinstance(request["arguments"], dict):
        raise MemoryFailure("invalid_service_request")
    access = Access(**request["access"])
    runtime.handshake(access)
    arguments = request["arguments"]
    operation = request["operation"]
    def selected_inbox() -> ProtectedInbox:
        if protected_inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        if 'inbox_id' not in arguments:
            return protected_inbox
        selected = arguments['inbox_id']
        if not isinstance(selected, str):
            raise MemoryFailure('invalid_arguments')
        if selected == protected_inbox.inbox_id:
            return protected_inbox
        if protected_history is None or selected not in protected_history:
            raise MemoryFailure('protected_inbox_not_found')
        return protected_history[selected]
    if operation == 'transcription-submit':
        if set(arguments) != {'material'} or not isinstance(arguments['material'], dict):
            raise MemoryFailure('invalid_arguments')
        from .transcription import Transcription, submit
        value = dict(arguments['material'])
        if not isinstance(value.get('source'), dict):
            raise MemoryFailure('invalid_arguments')
        value['source'] = Source(**value['source'])
        return submit(runtime, protected_inbox, access, Transcription(**value))
    if operation == 'transcription-read':
        if set(arguments) != {'message_id'} or not isinstance(arguments['message_id'], str):
            raise MemoryFailure('invalid_arguments')
        from .transcription import read
        return read(runtime, access, arguments['message_id'])
    if operation == 'protected-receive':
        if protected_inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        if (set(arguments) not in ({'event'}, {'event', 'stream_provenance'})
                or not isinstance(arguments['event'], dict)
                or ('stream_provenance' in arguments and arguments['stream_provenance'] is not True)):
            raise MemoryFailure('invalid_arguments')
        value = dict(arguments['event'])
        if value.get('sensitive') is not True:
            raise MemoryFailure('protected_explicit_mark_required')
        source = Source(**value.pop('source'))
        private_event = Event(source=source, **value)
        if private_event.completion != 'complete':
            raise MemoryFailure('protected_incomplete_unsupported')
        return protected_inbox.submit(access, private_event, stream_provenance=arguments.get('stream_provenance', False))
    if operation == 'protected-inboxes':
        runtime._require_scope(access, 'admin', 'protected_inboxes')
        if arguments:
            raise MemoryFailure('invalid_arguments')
        if protected_inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        inboxes = {'primary': protected_inbox.inbox_id, 'historical': sorted((protected_history or {}).keys())}
        runtime._require_scope(access, 'admin', 'protected_inboxes')
        return inboxes
    if operation == 'protected-backup':
        runtime._require_scope(access, 'admin', 'vault_backup')
        if protected_inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        if (set(arguments) != {'destination', 'backup_keys', 'timeout'}
                or any(not isinstance(arguments[name], str) or not 1 <= len(arguments[name]) <= 4096
                       for name in ('destination', 'backup_keys'))):
            raise MemoryFailure('invalid_vault_backup_request')
        if protected_history:
            from .vault_multi_backup import create_multi_backup
            return create_multi_backup(runtime, protected_inbox, (protected_inbox, *protected_history.values()), access,
                                       Path(arguments['destination']), Path(arguments['backup_keys']),
                                       timeout=arguments['timeout'], cancel=cancel)
        from .vault_backup import create_vault_backup
        return create_vault_backup(runtime, protected_inbox, access, Path(arguments['destination']),
                                   Path(arguments['backup_keys']), timeout=arguments['timeout'], cancel=cancel)
    if operation in {'protected-inventory', 'protected-links'}:
        if protected_inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        if set(arguments) not in ({'after', 'limit'}, {'after', 'limit', 'inbox_id'}):
            raise MemoryFailure('invalid_arguments')
        runtime._require_scope(access, 'admin', 'protected_inventory_select')
        inbox = selected_inbox()
        if operation == 'protected-inventory':
            return inbox.inventory(access, after=arguments['after'], limit=arguments['limit'])
        return inbox.inventory_links(access, after=arguments['after'], limit=arguments['limit'])
    if operation == 'protected-show-local':
        if protected_inbox is None or display_gate is None:
            raise MemoryFailure('protected_service_unconfigured')
        if (set(arguments) not in ({'object_id', 'purpose'}, {'object_id', 'purpose', 'inbox_id'}) or not isinstance(arguments['object_id'], str)
                or not isinstance(arguments['purpose'], str)):
            raise MemoryFailure('invalid_arguments')
        runtime._require_scope(access, 'recall', 'vault_local_display_select')
        from .service_vault import show
        return show(selected_inbox(), access, arguments['object_id'], arguments['purpose'], gate=display_gate, cancel=cancel)
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
        from .embedding import load_embedding
        expected = {"profile"} if operation == "mcp-list" else {"name", "arguments", "profile"}
        if set(arguments) != expected:
            raise MemoryFailure("invalid_arguments")
        # Discovery and raw capture do not use semantic vectors. A damaged
        # optional model config must not prevent saving original conversation.
        semantic_tools = {'evernook_v1_recall', 'evernook_v1_context', 'evernook_v1_status'}
        needs_embedding = operation == 'mcp' and arguments.get('name') in semantic_tools
        backend = None
        embedding_error = None
        if needs_embedding and embedding_config is not None:
            try:
                backend = embedding_binding.current() if embedding_binding is not None else load_embedding(embedding_config)
            except MemoryFailure as failure:
                if arguments.get('name') != 'evernook_v1_status':
                    raise
                embedding_error = failure.code
        operations, context, restricted = build_operations(runtime, access, profile=arguments["profile"],
            backend=backend, context_config=context_config, reflector=reflector, defer_context_validation=True,
            reflection_error=reflection_error)
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
        if name == 'evernook_v1_reflect' and reflector is not None:
            if reflection_binding is not None:
                reflection_binding.check()
            if embedding_binding is not None:
                embedding_binding.current()
            value = parsed.model_dump()
            value['topic_view_ids'] = tuple(value['topic_view_ids'])
            return reflector.reflect(access, **value, cancel=cancel,
                                     provider_check=reflection_binding.check if reflection_binding else None)
        if name == 'evernook_v1_status' and embedding_error is not None:
            status_result = invoke(parsed)
            assert isinstance(status_result, dict)
            status_result['semantic'] = {'state': 'unavailable', 'space_id': None, 'source_messages': None,
                'indexed_messages': None, 'pending_messages': None, 'source_watermark': None,
                'error_code': embedding_error, 'failed_chunks': None, 'stored_vectors': None}
            return status_result
        return invoke(parsed)
    if operation == "cli":
        from .cli import _execute, _parser
        if (set(arguments) != {"command", "input"} or arguments["command"] not in CLI_OPERATIONS
                or not isinstance(arguments["input"], dict)):
            raise MemoryFailure("operation_not_allowed")
        command = arguments["command"]
        if command in {'extract', 'extract-preferences'}:
            runtime._require_scope(access, 'maintain', 'service_extraction')
            if extractor is None or extraction_gate is None:
                raise MemoryFailure(extraction_error or 'extraction_service_unconfigured')
            if extraction_binding is not None:
                extraction_binding.check()
            value = dict(arguments['input'])
            if set(value) - {'source_ids', 'timeout'} or not isinstance(value.get('source_ids'), list):
                raise MemoryFailure('invalid_arguments')
            if not extraction_gate.acquire(blocking=False):
                raise MemoryFailure('extraction_service_busy')
            try:
                if extraction_binding is not None:
                    extraction_binding.check()
                from .extraction import ExtractionProcessor
                from .preferences import PreferenceProcessor
                processor = PreferenceProcessor(runtime, extractor) if command == 'extract-preferences' else ExtractionProcessor(runtime, extractor)
                value['source_ids'] = tuple(value['source_ids'])
                return processor.process(access, **value, cancel=cancel)
            finally:
                extraction_gate.release()
        if command == 'context':
            return dispatch(runtime, {**request, 'operation': 'mcp',
                'arguments': {'name': 'evernook_v1_context', 'profile': 'trusted-host',
                    'arguments': arguments['input']}}, cancel=cancel, context_config=context_config,
                embedding_config=embedding_config, reflector=reflector, embedding_binding=embedding_binding)
        if command in {'reflect', 'reflect-configure'}:
            if reflector is None:
                raise MemoryFailure(reflection_error or 'reflection_model_unconfigured')
            if reflection_binding is not None:
                reflection_binding.check()
            value = dict(arguments['input'])
            if 'provider_check' in value:
                raise MemoryFailure('invalid_arguments')
            if command == 'reflect-configure':
                if value:
                    raise MemoryFailure('invalid_arguments')
                return reflector.configure(access)
            if embedding_binding is not None:
                embedding_binding.current()
            if 'topic_view_ids' in value:
                if not isinstance(value['topic_view_ids'], list):
                    raise MemoryFailure('invalid_arguments')
                value['topic_view_ids'] = tuple(value['topic_view_ids'])
            return reflector.reflect(access, **value, cancel=cancel,
                                     provider_check=reflection_binding.check if reflection_binding else None)
        parser_arguments = [command, "--config", str(runtime.root / "runtime.json"),
            "--access", str(runtime.root / "unused-service-access.json")]
        if command in ("read", "state"):
            if set(arguments["input"]) != {"message_id"} or not isinstance(arguments["input"]["message_id"], str):
                raise MemoryFailure("invalid_arguments")
            parser_arguments += ["--message-id", arguments["input"]["message_id"]]
        namespace = _parser().parse_args(parser_arguments)
        if command in ("recall", "manage") or command.startswith('topic-view-'):
            namespace.embedding_config = embedding_config
        result = _execute(namespace, input_value=arguments["input"],
            service_runtime=runtime, service_access=access, service_cancel=cancel,
            service_embedding=embedding_binding.current if embedding_binding is not None else None)
        if command == "experience-decide":
            from .experience import ExperienceRecord

            assert isinstance(result, ExperienceRecord)
            return {"experience_id": result.experience_id, "revision": result.revision,
                "content_revision": result.content_revision, "state": result.state, "recorded_at": result.recorded_at,
                "confirmation_id": result.confirmation_id, "confirmation_version": result.confirmation_version,
                "request_id": result.request_id}
        if (command == 'manage' and arguments['input'] == {'action': 'read', 'view': 'status'}
                and isinstance(result, dict)):
            from .reflection import RemoteModel
            from .embedding import load_embedding
            from .host_context import HostContext

            def config_validation(path: Path | None, *, context: bool) -> str:
                if path is None:
                    return 'not_configured'
                try:
                    if context:
                        HostContext(runtime, SemanticRecall(runtime), path).status()
                    else:
                        load_embedding(path)
                        if embedding_binding is not None:
                            embedding_binding.current()
                except MemoryFailure as failure:
                    if failure.code == 'embedding_config_changed':
                        return 'restart_required'
                    return 'invalid'
                return 'valid'

            def provider_validation(binding: _ProviderBinding | None, error: str | None) -> str:
                if error is not None:
                    return 'invalid'
                if binding is None:
                    return 'not_configured'
                try:
                    binding.check()
                except MemoryFailure:
                    return 'restart_required'
                return 'valid'

            reflection_model = reflector.model if reflector is not None else None
            reflection_config_state = provider_validation(reflection_binding, reflection_error)
            extraction_config_state = provider_validation(extraction_binding, extraction_error)
            embedding_config_state = config_validation(embedding_config, context=False)
            from .embedding import LocalMiniLM
            from .extraction import LocalQwen
            from .reflection_model import LocalReflectionModel
            from .reflection_remote import RemoteReflectionModel

            def identity(provider: str, model: str | None, version: str) -> dict[str, str | None] | None:
                # Processor versions contain a fixed prefix and SHA-256, never raw configuration.
                digest = version.rsplit(':', 1)[-1]
                if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
                    return None
                return {'provider': provider, 'model': model, 'version': digest}

            identities: dict[str, dict[str, str | None] | None] = {'embedding': None, 'extraction': None, 'reflection': None}
            if embedding_config_state == 'valid' and embedding_binding is not None and type(embedding_binding.backend) is LocalMiniLM:
                identities['embedding'] = identity('local-minilm', 'paraphrase-multilingual-minilm-l12-v2', embedding_binding.backend.space.key)
            if extraction_config_state == 'valid' and type(extractor) is LocalQwen:
                identities['extraction'] = identity('local-qwen', 'qwen3-0.6b', extractor.processor_version)
            if reflection_config_state == 'valid' and type(reflection_model) is LocalReflectionModel:
                identities['reflection'] = identity('local-qwen', 'qwen3-0.6b', reflection_model.processor_version)
            elif reflection_config_state == 'valid' and type(reflection_model) is RemoteReflectionModel:
                # A keyed digest avoids exposing or enabling offline guesses of user-supplied model IDs or endpoints.
                opaque = hmac.digest(runtime._fingerprint_key, b'provider-identity-v1\0'
                                     + reflection_model.processor_version.encode(), hashlib.sha256).hex()
                identities['reflection'] = identity('openai-compatible', None, opaque)
            if reflection_config_state == 'restart_required':
                budget_status: dict[str, Any] = {'state': 'restart_required'}
            elif reflection_error is not None:
                budget_status = {'state': 'unavailable'}
            elif isinstance(reflector, ReflectionEngine):
                budget_status = reflector.daily_budget_status(access)
            else:
                budget_status = {'state': 'disabled'}
            return {**result, 'model_configuration': {
                'embedding': 'configured_local' if embedding_config is not None else 'not_configured',
                'reflection': 'configured_unavailable' if reflection_error is not None else 'not_configured' if reflection_model is None else 'configured_remote' if isinstance(reflection_model, RemoteModel) else 'configured_local',
                'extraction': 'configured_unavailable' if extraction_error is not None else 'configured_local' if extractor is not None else 'not_configured',
                'speech': 'host_owned'}, 'model_identity': identities, 'configuration_validation': {
                    'embedding': embedding_config_state,
                    'context': config_validation(context_config, context=True),
                    'extraction': extraction_config_state,
                    'reflection': reflection_config_state}, 'reflection_daily_budget': budget_status}
        return result
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
        from .embedding import load_embedding
        backend = (embedding_binding.current() if embedding_binding is not None else load_embedding(embedding_config)) if embedding_config is not None else None
        return SemanticRecall(runtime, backend).recall(access, **arguments, cancel=cancel)
    if operation == "status" and not arguments:
        return {"capabilities": runtime.handshake(access), "ledger": asdict(runtime.status(access)),
            "lexical": asdict(runtime.lexical_status(access))}
    raise MemoryFailure("unknown_service_operation")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, runtime: MemoryRuntime, key: bytes, *,
                 context_config: Path | None = None, embedding_config: Path | None = None, reflection_config: Path | None = None,
                 extraction_config: Path | None = None):
        self.runtime = runtime
        self.key = key
        self.single_writer = False
        self.protected_inbox: ProtectedInbox | None = None
        self.protected_history: dict[str, ProtectedInbox] = {}
        self.protected_state = 'unconfigured'
        self.display_gate = threading.BoundedSemaphore(1)
        self.context_config = context_config
        self.embedding_config = embedding_config
        self.embedding_binding = ServiceEmbedding(embedding_config) if embedding_config is not None else None
        self.extractor: ExtractorBackend | None = None
        self.extraction_error: str | None = None
        self.extraction_binding: _ProviderBinding | None = None
        self.extraction_gate = threading.BoundedSemaphore(1)
        if extraction_config is not None:
            from .extraction import _extraction_from_config
            try:
                self.extraction_binding = _ProviderBinding(extraction_config, 'extraction')
                self.extractor = _extraction_from_config(self.extraction_binding.config)
            except MemoryFailure as failure:
                self.extraction_error = failure.code
        self.reflector: ReflectionEngine | None = None
        self.reflection_error: str | None = None
        self.reflection_binding: _ProviderBinding | None = None
        if reflection_config is not None:
            from .reflection_model import _from_config
            try:
                self.reflection_binding = _ProviderBinding(reflection_config, 'reflection')
                self.reflector = _from_config(runtime, SemanticRecall(runtime,
                    self.embedding_binding.current() if self.embedding_binding else None), self.reflection_binding.config)
            except MemoryFailure as failure:
                self.reflection_error = failure.code
        self.slots = threading.BoundedSemaphore(8)
        self.serial_sessions = threading.BoundedSemaphore(4)
        self.connections = threading.BoundedSemaphore(12)
        self.stopping = threading.Event()
        from .service_maintenance import ServiceMaintenance
        self.maintenance: ServiceMaintenance | None = None
        super().__init__(("127.0.0.1", 0), _Handler)

    def maintenance_status(self) -> dict[str, Any]:
        return self.maintenance.snapshot() if self.maintenance else {"enabled": False}

    def server_close(self) -> None:
        self.stopping.set()
        try:
            super().server_close()
        finally:
            if self.embedding_binding is not None:
                self.embedding_binding.close()

    def process_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default traceback can contain private request values.
        pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        if not server.slots.acquire(blocking=False):
            return
        active = True
        serial = False
        authenticated = False
        deadline = time.monotonic() + 5
        try:
            session = authenticate_server(self.request, server.key, deadline)
            authenticated = True
            request = receive_json(self.request, deadline)
            if request != OPEN_SERIAL:
                self._respond(server, session, request)
                return
            serial = server.serial_sessions.acquire(blocking=False)
            send_json(self.request, SERIAL_READY if serial else SINGLE_READY, deadline)
            if not serial:
                self._respond(server, session, receive_json(self.request, deadline))
                return
            while not server.stopping.is_set():
                server.slots.release()
                active = False
                idle_until = time.monotonic() + SERIAL_IDLE
                while not select.select([self.request], [], [], 0.2)[0]:
                    if server.stopping.is_set() or time.monotonic() >= idle_until:
                        return
                if not self.request.recv(1, socket.MSG_PEEK):
                    return
                active = server.slots.acquire(blocking=False)
                if not active:
                    send_json(self.request, {"version": 1, "ok": False, "error": {"code": "service_busy"}},
                              time.monotonic() + 5)
                    return
                request = receive_json(self.request, time.monotonic() + 5)
                if not serial_operation(request.get("operation"), request.get("arguments")):
                    send_json(self.request, {"version": 1, "ok": False, "error": {"code": "operation_not_allowed"}},
                              time.monotonic() + 5)
                    return
                self._respond(server, session, request)
        except MemoryFailure as error:
            if authenticated:
                try:
                    send_json(self.request, {"version": 1, "ok": False, "error": {"code": error.code}},
                              time.monotonic() + 5)
                except (OSError, MemoryFailure):
                    pass
        except OSError:
            pass
        finally:
            if active:
                server.slots.release()
            if serial:
                server.serial_sessions.release()

    def _respond(self, server: _Server, session: bytes, request: dict[str, Any]) -> None:
        private = False
        try:
            if 'sealed' in request:
                request = open_private_frame(session, request, direction='request')
                private = True
                if request.get('operation') not in PRIVATE_OPERATIONS:
                    raise MemoryFailure('invalid_protected_frame')
            elif request.get('operation') in PRIVATE_OPERATIONS:
                raise MemoryFailure('protected_encryption_required')
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
                    context_config=server.context_config, embedding_config=server.embedding_config, reflector=server.reflector,
                    protected_inbox=server.protected_inbox, protected_history=server.protected_history, display_gate=server.display_gate,
                    extractor=server.extractor, extraction_gate=server.extraction_gate, embedding_binding=server.embedding_binding,
                    extraction_error=server.extraction_error, reflection_error=server.reflection_error,
                    extraction_binding=server.extraction_binding, reflection_binding=server.reflection_binding)
            finally:
                done.set()
                watcher.join()
            is_status = request.get("operation") == "status" or (
                request.get("operation") == "mcp" and request.get("arguments", {}).get("name") == "evernook_v1_status")
            if is_status and isinstance(result, dict):
                result["service"] = {"independent": True, "single_writer": server.single_writer,
                    "protected_capture": server.protected_inbox is not None,
                    "protected_state": server.protected_state,
                    "maintenance": server.maintenance_status()}
                if isinstance(result.get("processing"), dict):
                    result["processing"]["consumer_running"] = server.maintenance is not None
            elif (request.get('operation') == 'cli' and isinstance(result, dict)
                    and request.get('arguments', {}).get('command') == 'manage'
                    and request.get('arguments', {}).get('input') == {'action': 'read', 'view': 'status'}):
                result['protected_state'] = server.protected_state
            from .serialization import result_value
            result = result_value(result)
            response: dict[str, object] = {"version": 1, "ok": True, "result": result}
        except MemoryFailure as error:
            response = {"version": 1, "ok": False, "error": {"code": error.code}}
        except (ValueError, TypeError, KeyError):
            response = {"version": 1, "ok": False, "error": {"code": "invalid_arguments"}}
        except Exception:
            response = {"version": 1, "ok": False, "error": {"code": "service_operation_failed"}}
        try:
            send_json(self.request, seal_private_frame(session, response, direction='response') if private else response,
                      time.monotonic() + 5)
        except MemoryFailure:
            # An unsendable reply closes the transport, including sealed replies.
            raise ConnectionError from None


def serve(config: Path, *, context_config: Path | None = None, embedding_config: Path | None = None, reflection_config: Path | None = None,
          extraction_config: Path | None = None,
          maintenance_access: Path | None = None, maintenance_interval: float = 5,
          navigation_policies: tuple[str, ...] = (), activate_owner: Path | None = None,
          allow_legacy_shared: bool = False, protected_root: Path | None = None,
          protected_keys: Path | None = None, activate_verified_inbox: bool = False,
          activate_restored_without_vault: bool = False, protected_history_roots: tuple[Path, ...] = (),
          protected_history_keys: tuple[Path, ...] = (), activate_verified_multi: bool = False) -> None:
    if type(allow_legacy_shared) is not bool or (activate_owner is not None and allow_legacy_shared):
        raise MemoryFailure('invalid_service_mode')
    if (protected_root is None) != (protected_keys is None) or (protected_root is not None and allow_legacy_shared):
        raise MemoryFailure('invalid_protected_service_config')
    if type(activate_verified_inbox) is not bool or (activate_verified_inbox and
            (activate_owner is None or protected_root is None or protected_keys is None or allow_legacy_shared)):
        raise MemoryFailure('invalid_protected_service_config')
    if type(activate_restored_without_vault) is not bool or (activate_restored_without_vault and
            (activate_owner is None or protected_root is not None or protected_keys is not None
             or allow_legacy_shared or activate_verified_inbox)):
        raise MemoryFailure('invalid_protected_service_config')
    if (type(activate_verified_multi) is not bool or len(protected_history_roots) != len(protected_history_keys)
            or len(protected_history_roots) > 15 or (protected_history_roots and protected_root is None)
            or (activate_verified_multi and (activate_owner is None or protected_root is None
                or not protected_history_roots or allow_legacy_shared or activate_verified_inbox
                or activate_restored_without_vault))):
        raise MemoryFailure('invalid_protected_service_config')
    runtime = MemoryRuntime(config, service_owner=True)
    from .runtime import _load
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
            if next(runtime.root.glob('vault-destruction-*.json'), None) is not None:
                from .vault_destroy import require_no_pending
                require_no_pending(runtime)
            key = secrets.token_bytes(32)
            with _Server(runtime, key, context_config=context_config, embedding_config=embedding_config, reflection_config=reflection_config,
                         extraction_config=extraction_config) as server:
                # First cutover and explicit legacy mode retain their strict
                # startup contract. Only an already activated ledger may restart
                # with an unavailable optional provider and keep raw capture.
                from .runtime import _load
                if (activate_owner is not None or not _load(config).get('service_mode')) and (server.extraction_error or server.reflection_error):
                    raise MemoryFailure(server.extraction_error or server.reflection_error or 'service_provider_unavailable')
                from .missing_vault import attachment, read_state, record_state
                from .service_bootstrap import inspect_vault_startup
                from .runtime_mode import runtime_mode
                from .vault_retirement import active_registry, active_link_count
                mode, missing_vault, attached_vault = inspect_vault_startup(runtime, config,
                    protected_root=protected_root, protected_keys=protected_keys, protected_history_roots=protected_history_roots,
                    activate_owner=activate_owner, activate_verified_inbox=activate_verified_inbox,
                    activate_verified_multi=activate_verified_multi, activate_restored_without_vault=activate_restored_without_vault,
                    allow_legacy_shared=allow_legacy_shared)
                multi_marker, single_marker = mode == "multi", mode == "single"
                if missing_vault:
                    server.protected_state = 'restored_missing'
                if protected_root is not None and protected_keys is not None:
                    try:
                        from .protected import ProtectedInbox
                    except ImportError:
                        raise MemoryFailure('vault_dependency_missing') from None
                    server.protected_inbox = ProtectedInbox(runtime, protected_root, protected_keys)
                    server.protected_state = 'configured'
                    if single_marker:
                        from .vault_migration import _materials
                        from .vault_single_binding import read as read_single_binding
                        bound = read_single_binding(runtime)
                        if (bound is None or server.protected_inbox.inbox_id != bound['inbox_id']
                                or server.protected_inbox.public['key_id'] != bound['key_id']
                                or _materials(server.protected_inbox) != bound['material']):
                            raise MemoryFailure('protected_single_binding_mismatch')
                    if protected_history_roots:
                        history = tuple(ProtectedInbox(runtime, root, keys) for root, keys in
                                        zip(protected_history_roots, protected_history_keys))
                        all_inboxes = (server.protected_inbox, *history)
                        if activate_verified_multi:
                            assert activate_owner is not None
                            from .vault_multi_binding import activate
                            activate(runtime, runtime.access_from_file(activate_owner), server.protected_inbox, all_inboxes)
                            multi_marker = True
                        elif multi_marker:
                            from .vault_multi_binding import verify_startup
                            verify_startup(runtime, server.protected_inbox, all_inboxes)
                        server.protected_inbox._historical_inboxes = history
                        server.protected_history = {item.inbox_id: item for item in history}
                    elif multi_marker:
                        raise MemoryFailure('protected_multi_binding_mismatch')
                    if attached_vault is not None:
                        from .vault_migration import _materials, _snapshot
                        if (server.protected_inbox.inbox_id != attached_vault['inbox_id']
                                or server.protected_inbox.public['key_id'] != attached_vault['key_id']
                                or _materials(server.protected_inbox) != attached_vault['material']):
                            raise MemoryFailure('protected_attachment_mismatch')
                        deadline = time.monotonic() + 120
                        def remaining_attachment_time() -> float:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise MemoryFailure('protected_migration_timeout')
                            return remaining
                        with runtime._connection(ownership_exclusive=True, check=remaining_attachment_time) as core:
                            core.execute('BEGIN IMMEDIATE')
                            if attachment(runtime, core) != attached_vault:
                                raise MemoryFailure('protected_attachment_mismatch')
                            with server.protected_inbox._connection(gate_db=core) as store:
                                store.execute('BEGIN IMMEDIATE')
                                snapshot = _snapshot(runtime, server.protected_inbox, core, store, private=None,
                                                     deadline=deadline, require_readable_entries=False,
                                                     allow_pending=True, max_objects=None)
                                if snapshot['material'] != attached_vault['material']:
                                    raise MemoryFailure('protected_attachment_mismatch')
                                store.execute('COMMIT')
                            core.execute('COMMIT')
                if maintenance_access is not None:
                    from .service_maintenance import ServiceMaintenance
                    server.maintenance = ServiceMaintenance(runtime, maintenance_access, maintenance_interval,
                        navigation_policies, server.stopping, embedding_config=embedding_config,
                        embedding_binding=server.embedding_binding)
                if activate_owner is not None and not activate_verified_multi:
                    owner = runtime.access_from_file(activate_owner)
                    runtime._require_scope(owner, "admin", "activate_service")
                    with runtime._connection(ownership_exclusive=True) as db:
                        db.execute("BEGIN EXCLUSIVE")
                        if 'admin' not in runtime._authorize(owner).scopes:
                            raise MemoryFailure('permission_denied')
                        if activate_restored_without_vault:
                            record_state(runtime, db)
                            # The marker commits before service_mode. A crash leaves a
                            # retryable unactivated ledger, never an unmarked active one.
                            db.execute('COMMIT')
                            db.execute('BEGIN EXCLUSIVE')
                            if 'admin' not in runtime._authorize(owner).scopes or not read_state(runtime, db):
                                raise MemoryFailure('permission_denied')
                            value = _load(config)
                            _atomic_json(config, {**value, 'service_mode': True, 'restored_missing_vault': True})
                            db.execute('COMMIT')
                            server.protected_state = 'restored_missing'
                        elif activate_verified_inbox:
                            if _load(config).get('service_mode') is True:
                                raise MemoryFailure('invalid_service_mode')
                            assert server.protected_inbox is not None
                            from .vault_migration import verify_for_activation, _materials
                            from .vault_single_binding import write as write_single_binding
                            with server.protected_inbox._connection(gate_db=db) as store:
                                store.execute('BEGIN IMMEDIATE')
                                verify_for_activation(runtime, server.protected_inbox, owner, db, store)
                                single_content = {'version': 1, 'inbox_id': server.protected_inbox.inbox_id,
                                                  'key_id': str(server.protected_inbox.public['key_id']),
                                                  'inbox_dir': str(external(protected_root)) if protected_root else '',
                                                  'key_dir': str(external(protected_keys)) if protected_keys else '',
                                                  'material': _materials(server.protected_inbox)}
                                write_single_binding(runtime, single_content)
                                value = _load(config)
                                _atomic_json(config, {**value, "service_mode": True, "protected_single_binding": True})
                                # The binding marker commits before service_mode; keep the
                                # startup invariant check consistent with what it will read.
                                single_marker = True
                                db.execute("COMMIT")
                                store.execute('COMMIT')
                        else:
                            known = active_registry(runtime, db)
                            expected = ({server.protected_inbox.inbox_id: str(server.protected_inbox.public['key_id'])}
                                        if server.protected_inbox is not None else {})
                            if any(expected.get(inbox_id) != key_id for inbox_id, key_id in known.items()):
                                raise MemoryFailure('protected_cutover_requires_migration')
                            if active_link_count(runtime, db):
                                raise MemoryFailure('protected_cutover_requires_migration')
                            if server.protected_inbox is not None:
                                with server.protected_inbox._connection(gate_db=db) as inbox:
                                    if inbox.execute('SELECT 1 FROM objects LIMIT 1').fetchone():
                                        raise MemoryFailure('protected_cutover_requires_migration')
                            value = _load(config)
                            _atomic_json(config, {**value, "service_mode": True})
                            db.execute("COMMIT")
                with runtime._connection(ownership_exclusive=True) as db:
                    current_mode = runtime_mode(_load(config))
                    if ((current_mode == 'multi') != multi_marker
                            or (runtime.root / 'protected-multi-binding.json').exists() != multi_marker):
                        raise MemoryFailure('protected_multi_binding_mismatch')
                    if ((current_mode == 'single') != single_marker
                            or (runtime.root / 'protected-single-binding.json').exists() != single_marker):
                        raise MemoryFailure('protected_single_binding_mismatch')
                    activated = current_mode != 'embedded'
                    if read_state(runtime, db):
                        if server.protected_inbox is not None or allow_legacy_shared:
                            raise MemoryFailure('protected_restore_required')
                        server.protected_state = 'restored_missing'
                    if not activated:
                        if not allow_legacy_shared:
                            raise MemoryFailure('service_activation_required')
                        if active_registry(runtime, db):
                            raise MemoryFailure('protected_cutover_requires_migration')
                        if active_link_count(runtime, db):
                            raise MemoryFailure('protected_cutover_requires_migration')
                    server.single_writer = activated
                if not activated:
                    sys.stderr.write('evernook:legacy_shared_mode_not_single_writer\n')
                _atomic_json(runtime.root / ENDPOINT,
                    {"version": 1, "port": server.server_address[1], "key": key.hex()}, retry_replace=True)
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
    parser.add_argument("--reflection-config", type=Path)
    parser.add_argument("--extraction-config", type=Path)
    parser.add_argument("--maintenance-access", type=Path)
    parser.add_argument("--maintenance-interval", type=float, default=5)
    parser.add_argument("--navigation-policy", action="append", default=[])
    parser.add_argument("--activate-owner", type=Path)
    parser.add_argument("--allow-legacy-shared", action="store_true")
    parser.add_argument("--protected-inbox", type=Path)
    parser.add_argument("--protected-keys", type=Path)
    parser.add_argument('--activate-verified-inbox', action='store_true')
    parser.add_argument('--activate-restored-without-vault', action='store_true')
    parser.add_argument('--protected-history-inbox', type=Path, action='append', default=[])
    parser.add_argument('--protected-history-keys', type=Path, action='append', default=[])
    parser.add_argument('--activate-verified-multi', action='store_true')
    try:
        args = parser.parse_args()
        serve(args.config, context_config=args.context_config, embedding_config=args.embedding_config, reflection_config=args.reflection_config,
            extraction_config=args.extraction_config,
            maintenance_access=args.maintenance_access, maintenance_interval=args.maintenance_interval,
            navigation_policies=tuple(args.navigation_policy), activate_owner=args.activate_owner,
            allow_legacy_shared=args.allow_legacy_shared, protected_root=args.protected_inbox,
            protected_keys=args.protected_keys, activate_verified_inbox=args.activate_verified_inbox,
            activate_restored_without_vault=args.activate_restored_without_vault,
            protected_history_roots=tuple(args.protected_history_inbox), protected_history_keys=tuple(args.protected_history_keys),
            activate_verified_multi=args.activate_verified_multi)
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
