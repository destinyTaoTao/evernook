"""Certify a count only against an exhaustively checked, authorized ledger snapshot."""
from __future__ import annotations

import math
import threading
import time

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure, OriginalCount, OriginalCountScope
from .observations import source_contexts
from .runtime import MemoryRuntime, _identifier, _MEMORY_EVIDENCE

_MAX_ORIGINALS = 32
_MAX_SPANS = 100
_MAX_QUOTE_CHARS = 16_384
_INCOMPLETE = OriginalCount('abstain', None, 'incomplete_evidence')
_BUDGET = OriginalCount('abstain', None, 'evidence_budget')


def count_user_originals(runtime: MemoryRuntime, access: Access, scope: OriginalCountScope,
                         evidence: tuple[EvidenceSpan, ...], *, timeout: float,
                         cancel: threading.Event | None) -> OriginalCount:
    if (not isinstance(scope, OriginalCountScope) or not isinstance(evidence, tuple)
            or type(timeout) not in (int, float) or not 0 < timeout <= 30 or not math.isfinite(timeout)
            or (cancel is not None and not isinstance(cancel, threading.Event))):
        raise MemoryFailure('invalid_original_count_request')
    deadline = time.monotonic() + timeout

    def check() -> float:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('original_count_canceled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MemoryFailure('original_count_timeout')
        return remaining

    try:
        check()
        runtime._require_scope(access, 'recall', 'count_user_originals')
        check()
        for value in (scope.entry, scope.session, scope.branch):
            _identifier(value)
        if not evidence:
            return _INCOMPLETE
        if len(evidence) > _MAX_SPANS:
            return _BUDGET
        provided: set[tuple[str, int, int]] = set()
        chars = 0
        for span in evidence:
            check()
            if (not isinstance(span, EvidenceSpan) or span.representation != 'original'
                    or any(type(value) is not int for value in (span.state_version, span.content_version, span.start, span.end))
                    or not isinstance(span.expected_text, str) or not 0 <= span.start < span.end
                    or span.state_version < 1 or span.content_version < 1):
                raise MemoryFailure('invalid_original_count_request')
            _identifier(span.message_id)
            chars += len(span.expected_text)
            if chars > _MAX_QUOTE_CHARS:
                return _BUDGET
            provided.add((span.message_id, span.state_version, span.content_version))

        with runtime._connection(check=check) as db:
            db.execute('BEGIN')
            # The source column holds the stable source identity, independent of the raw message body.
            rows = db.execute(f'''SELECT messages.id, coalesce(v.version,1),
                    coalesce(json_extract(messages.receipt,'$.content_version'),1)
                FROM messages LEFT JOIN visibility v ON v.message_id=messages.id
                WHERE json_extract(messages.source,'$.entry')=? AND json_extract(messages.source,'$.session')=?
                AND json_extract(messages.source,'$.branch')=? AND coalesce(v.status,'active')='active'
                AND json_extract(messages.event,'$.role')='user' AND json_extract(messages.event,'$.completion')='complete'
                AND json_extract(messages.event,'$.text')<>''
                AND json_extract(messages.receipt,'$.original_saved')=1 AND json_extract(messages.receipt,'$.representation')='original'
                AND coalesce(json_extract(messages.event,'$.sensitive'),0)=0
                AND coalesce(json_extract(messages.event,'$.protected_source'),0)=0 AND {_MEMORY_EVIDENCE}
                LIMIT ?''', (scope.entry, scope.session, scope.branch, _MAX_ORIGINALS + 1)).fetchall()
            if len(rows) > _MAX_ORIGINALS:
                return _BUDGET
            if {(row[0], row[1], row[2]) for row in rows} != provided:
                return _INCOMPLETE
            for span in evidence:
                check()
                source_contexts(db, (span,), 'original_count')
        runtime._require_scope(access, 'recall', 'count_user_originals_handoff')
        check()
    except MemoryFailure as error:
        if error.code == 'original_count_timeout':
            return OriginalCount('abstain', None, 'timeout')
        if error.code in {'invalid_observation_span', 'observation_span_mismatch', 'stale_observation_source'}:
            return _INCOMPLETE
        raise
    return OriginalCount('answer', len(rows), 'scope_verified')
