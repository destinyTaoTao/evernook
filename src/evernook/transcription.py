"""Trusted-host ASR material; recognition and audio remain outside EverNook."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
import math
import re
import sqlite3
from typing import Any, TYPE_CHECKING

from .models import Access, AttachmentInput, Capture, Event, MemoryFailure, SearchHit, Source
from .runtime import MemoryRuntime, _identifier, _json, _unique_object, _RECALLABLE
from .safety import credential_signal

if TYPE_CHECKING:
    from .protected import ProtectedInbox

MARKER = 'evernook_asr_transcript_v1'
ATTACHMENT = 'evernook_asr_metadata_v1'


def _safe_label(value: object, *, model: bool = False) -> bool:
    if (not isinstance(value, str) or not value.strip() or len(value) > 128 or '\\' in value
            or value.startswith(('/', '~')) or '://' in value or re.match(r'^[A-Za-z]:', value)
            or any(part in {'', '.', '..'} for part in value.split('/'))
            or (not model and '/' in value) or credential_signal(value)):
        return False
    try:
        _identifier(value)
    except MemoryFailure:
        return False
    return True


@dataclass(frozen=True)
class Transcription:
    source: Source
    sequence: int
    occurred_at: str | None
    completion: str
    text: str = field(repr=False)
    provider: str
    model: str
    language: str
    execution: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    sensitive: bool = False
    speaker: str = 'unknown'


@dataclass(frozen=True)
class TranscriptionHit(SearchHit):
    transcription: dict[str, Any]


def provenance(db: sqlite3.Connection, message_id: str) -> dict[str, Any] | None:
    row = db.execute('SELECT manifest FROM captures WHERE message_id=?', (message_id,)).fetchone()
    if row is None:
        # A missing capture could have belonged to a transcript; do not present
        # any tool output as a verified user statement when provenance is lost.
        return {'kind': 'tool_output', 'confirmation': 'unverified', 'metadata': 'unavailable'}
    try:
        manifest = json.loads(row['manifest'], object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict):
            raise ValueError()
        if manifest.get('tool_name') != MARKER:
            return None
        unavailable = {'kind': 'asr_transcription', 'confirmation': 'unverified', 'metadata': 'unavailable'}
        receipt = MemoryRuntime._capture_receipt(row['manifest'])
        info = next((item for item in receipt.attachments if item.attachment_id == ATTACHMENT), None)
        if info is None or not info.saved:
            return unavailable
        payload = MemoryRuntime._attachment_data(db, message_id, info)
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {'format', 'version', 'provider', 'model', 'language',
                'execution', 'start_ms', 'end_ms', 'confidence', 'confirmation', 'speaker'}
                or value['format'] != 'evernook-asr-transcription' or type(value['version']) is not int
                or value['version'] != 1 or value['confirmation'] != 'unverified'
                or not isinstance(value['speaker'], str) or value['speaker'] not in {'user', 'assistant', 'unknown'}
                or not isinstance(value['execution'], str) or value['execution'] not in {'local', 'remote'}
                or any(not _safe_label(value[key], model=key == 'model') for key in ('provider', 'model', 'language'))
                or type(value['start_ms']) is not int or type(value['end_ms']) is not int
                or not 0 <= value['start_ms'] <= value['end_ms'] <= 86400000
                or (value['confidence'] is not None and (type(value['confidence']) not in (int, float)
                    or not 0 <= value['confidence'] <= 1 or not math.isfinite(value['confidence'])))):
            return unavailable
        return {'kind': 'asr_transcription', **value}
    except (ValueError, TypeError, KeyError, UnicodeError, MemoryFailure):
        # Broken manifest cannot safely be labeled as an ordinary user statement.
        return {'kind': 'asr_transcription', 'confirmation': 'unverified', 'metadata': 'unavailable'}


def annotate(db: sqlite3.Connection, hit: SearchHit) -> SearchHit:
    if hit.role != 'tool':
        return hit
    value = provenance(db, hit.message_id)
    return TranscriptionHit(**vars(hit), transcription=value) if value is not None else hit


def read(runtime: MemoryRuntime, access: Access, message_id: str) -> dict[str, Any] | None:
    runtime._require_scope(access, 'recall', 'read_transcription')
    _identifier(message_id)
    with runtime._connection() as db:
        db.execute('BEGIN')
        row = db.execute(f'SELECT messages.event,messages.receipt FROM messages WHERE messages.id=? AND {_RECALLABLE}',
                         (message_id,)).fetchone()
        if row is None:
            return None
        stored = runtime._stored(row)
        if not stored.receipt.original_saved or stored.receipt.representation != 'original':
            return None
        value = provenance(db, message_id) if stored.event.role == 'tool' else None
        if value is None:
            return None
        return {'message': asdict(stored), 'transcription': value}


def submit(runtime: MemoryRuntime, inbox: ProtectedInbox | None, access: Access, material: Transcription) -> dict[str, Any]:
    if not isinstance(material, Transcription) or type(material.sensitive) is not bool:
        raise MemoryFailure('invalid_transcription')
    for key, value in (('provider', material.provider), ('model', material.model), ('language', material.language)):
        if not _safe_label(value, model=key == 'model'):
            raise MemoryFailure('invalid_transcription')
    if (not isinstance(material.execution, str) or material.execution not in {'local', 'remote'}
            or not isinstance(material.speaker, str) or material.speaker not in {'user', 'assistant', 'unknown'}
            or type(material.start_ms) is not int
            or type(material.end_ms) is not int or not 0 <= material.start_ms <= material.end_ms <= 86400000
            or (material.confidence is not None and (type(material.confidence) not in (int, float)
                or not 0 <= material.confidence <= 1 or not math.isfinite(material.confidence)))):
        raise MemoryFailure('invalid_transcription')
    event = Event(material.source, 'tool', material.sequence, material.occurred_at, material.completion, material.text)
    runtime._validate_event(event)
    metadata: dict[str, Any] = {'format': 'evernook-asr-transcription', 'version': 1,
        'provider': material.provider, 'model': material.model, 'language': material.language,
        'execution': material.execution, 'start_ms': material.start_ms, 'end_ms': material.end_ms,
        'confidence': material.confidence, 'confirmation': 'unverified', 'speaker': material.speaker}
    protected = material.sensitive or credential_signal(material.text)
    if protected:
        if material.completion != 'complete':
            raise MemoryFailure('protected_incomplete_unsupported')
        if inbox is None:
            raise MemoryFailure('protected_service_unconfigured')
        payload = _json({'format': 'evernook-protected-transcription', 'version': 1,
                         'metadata': metadata, 'text': material.text})
        private_receipt = inbox.submit(access, replace(event, text=payload, sensitive=True, protected_source=True))
        return {'kind': 'transcription', 'storage': 'protected', 'receipt': asdict(private_receipt)}
    # Partial outputs remain explicitly projected, not recalled as complete speech.
    event = replace(event, protected_source=material.completion != 'complete')
    attachment = AttachmentInput(ATTACHMENT, MARKER, 'application/json', (_json(metadata).encode('utf-8'),))
    receipt = runtime.submit_capture(access, Capture(event, 'message', tool_name=MARKER, attachments=(attachment,)))
    return {'kind': 'transcription', 'storage': 'ordinary', 'receipt': asdict(receipt)}
