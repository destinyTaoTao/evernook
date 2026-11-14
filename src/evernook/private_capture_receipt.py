"""Fail-closed preflight and non-secret receipts for trusted private capture."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import re
import threading
from typing import Any, Protocol
from uuid import UUID

from .models import Access, AttachmentInput, Capture, Event, MemoryFailure


class _Client(Protocol):
    def call(self, access: Access, operation: str, arguments: dict[str, Any], *,
             timeout: float = 30, cancel: threading.Event | None = None) -> Any: ...


def capture(client: _Client, access: Access, request: Capture, *, timeout: float = 30,
            cancel: threading.Event | None = None) -> dict[str, Any]:
    """No unsupported content is sent. Unknown means an attempted send may have committed."""
    if (not isinstance(request, Capture) or not isinstance(request.event, Event)
            or not isinstance(request.kind, str) or request.kind not in {'message', 'tool_call', 'tool_result'} or type(request.provided) is not bool
            or not isinstance(request.attachments, tuple) or len(request.attachments) > 16
            or any(not isinstance(item, AttachmentInput) for item in request.attachments)
            or (request.text_chunks is not None and (not isinstance(request.text_chunks, tuple)
                or len(request.text_chunks) > 4096 or any(not isinstance(item, str) for item in request.text_chunks)))):
        raise MemoryFailure('invalid_private_capture')
    if request.event.sensitive is not True:
        raise MemoryFailure('protected_explicit_mark_required')
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise MemoryFailure('invalid_service_timeout')
    if cancel is not None and cancel.is_set():
        return {'status': 'not_submitted', 'gaps': ['canceled_before_submission'], 'protected_original_saved': False}
    gaps: list[str] = []
    if request.attachments:
        gaps.append('private_attachments_unsupported')
    if request.text_chunks is not None:
        gaps.append('private_stream_unsupported')
    if request.event.completion != 'complete':
        gaps.append('incomplete_output')
    if not request.provided:
        gaps.append('entry_not_provided')
    if request.kind != 'message' or request.input_id is not None or request.tool_name is not None or request.event.role == 'tool':
        gaps.append('private_tool_metadata_unsupported')
    if gaps:
        return {'status': 'not_submitted', 'gaps': gaps, 'protected_original_saved': False}
    if cancel is not None and cancel.is_set():
        return {'status': 'not_submitted', 'gaps': ['canceled_before_submission'], 'protected_original_saved': False}
    try:
        result = client.call(access, 'protected-receive', {'event': asdict(request.event)}, timeout=timeout, cancel=cancel)
        message = result.get('message') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('state') != 'protected_linked'
                or result.get('encrypted_original_saved') is not True or not isinstance(message, dict)
                or type(result.get('version')) is not int or result['version'] != 1
                or not isinstance(result.get('key_id'), str) or not re.fullmatch(r'[0-9a-f]{64}', result['key_id'])
                or message.get('status') != 'projection_only' or type(message.get('content_version')) is not int
                or message['content_version'] != 1 or not isinstance(message.get('received_at'), str)
                or message.get('original_saved') is not False or message.get('representation') != 'projection'):
            raise ValueError()
        timestamp = datetime.fromisoformat(message['received_at'])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError()
        identifiers = [result.get('inbox_id'), result.get('object_id'), message.get('message_id')]
        if any(not isinstance(value, str) or str(UUID(value)) != value for value in identifiers):
            raise ValueError()
        return {'status': 'saved', 'gaps': [], 'protected_original_saved': True,
                'inbox_id': identifiers[0], 'object_id': identifiers[1], 'message_id': identifiers[2]}
    except Exception:
        return {'status': 'outcome_unknown', 'gaps': ['write_not_confirmed'], 'protected_original_saved': None}
