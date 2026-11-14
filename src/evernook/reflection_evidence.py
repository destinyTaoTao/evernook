"""Read-only, bounded reflection evidence tools. Never dispatch model-chosen operations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import json
import hashlib
from typing import Any

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .observations import source_contexts
from .runtime import MemoryRuntime, _MEMORY_EVIDENCE
from .semantic import SemanticRecall
from .topic_views import TopicViews
from .safety import credential_signal


class EvidenceTools:
    def __init__(self, runtime: MemoryRuntime, recall: SemanticRecall, access: Access, *, maximum: int,
                 chars: int, candidates: int, check: Callable[[], float], current_input_id: str | None = None):
        self.runtime, self.recall, self.access = runtime, recall, access
        self.maximum, self.chars, self.candidates, self.check = maximum, chars, candidates, check
        self.current_input_id = current_input_id
        self.evidence: list[dict[str, Any]] = []
        self.discovered: set[str] = set()
        self.views: dict[str, dict[str, Any]] = {}
        self.returned_chars = 0
        self.candidate_count = 0
        self.coverage: list[dict[str, Any]] = []

    @staticmethod
    def check_remote_input(runtime: MemoryRuntime, access: Access, message_id: str | None, check: Callable[[], float]) -> None:
        runtime._require_scope(access, 'recall', 'reflection_remote_input')
        check()
        if message_id is None:
            return
        with runtime._connection(check=check) as db:
            row = db.execute('SELECT event,receipt FROM messages WHERE id=?', (message_id,)).fetchone()
            if row is None:
                raise MemoryFailure('remote_reflection_private_input')
            stored = runtime._stored(row)
            if (stored.event.sensitive or stored.event.protected_source or not stored.receipt.original_saved
                    or runtime._state(db, message_id).status != 'active'):
                raise MemoryFailure('remote_reflection_private_input')

    def authorize_remote(self) -> None:
        self.check_remote_input(self.runtime, self.access, self.current_input_id, self.check)
        with self.runtime._connection(check=self.check) as db:
            db.execute('BEGIN')
            for item in self.evidence:
                self.check()
                span = EvidenceSpan(item['message_id'], item['state_version'], 'original', item['content_version'],
                    item['start'], item['end'], item['quote'])
                current = source_contexts(db, (span,), 'reflection_remote')[0]
                if current.get('transcription') != item.get('transcription'):
                    raise MemoryFailure('reflection_source_unavailable')
                if credential_signal(item['quote']):
                    raise MemoryFailure('protected_query')

    def admit(self, message_id: str, start: int = 0, length: int = 512) -> None:
        self.check()
        if message_id == self.current_input_id:
            return
        if message_id not in self.discovered:
            raise MemoryFailure('reflection_source_not_discovered')
        if type(start) is not int or start < 0 or type(length) is not int or not 1 <= length <= 2048:
            raise MemoryFailure('reflection_invalid_action')
        with self.runtime._connection(check=self.check) as db:
            db.execute('BEGIN')
            state = self.runtime._state(db, message_id)
            row = db.execute('SELECT event,receipt FROM messages WHERE id=?', (message_id,)).fetchone()
            stored = self.runtime._stored(row)
            if state.status != 'active' or start >= len(stored.event.text):
                return
            end = min(len(stored.event.text), start + length)
            span = EvidenceSpan(message_id, state.version, 'original', stored.receipt.content_version, start, end, stored.event.text[start:end])
            try:
                context = source_contexts(db, (span,), 'reflection_original')[0]
            except MemoryFailure as error:
                if error.code in {'stale_observation_source', 'invalid_observation_span'}:
                    return
                raise
        if any(all(item[key] == context[key] for key in ('message_id', 'start', 'end', 'state_version', 'content_version')) for item in self.evidence):
            return
        if len(self.evidence) >= self.maximum or self.returned_chars + len(span.expected_text) > self.chars:
            raise MemoryFailure('reflection_source_budget')
        context['evidence_id'] = 'e' + str(len(self.evidence) + 1)
        self.evidence.append(context)
        self.returned_chars += len(span.expected_text)

    def search(self, query: str, *, cancel: Any = None) -> None:
        self.runtime._require_scope(self.access, 'recall', 'reflection_search')
        left = self.candidates - self.candidate_count
        if left <= 0:
            raise MemoryFailure('reflection_candidate_budget')
        page = self.recall.recall(self.access, query, limit=min(left, 10), timeout=self.check(), cancel=cancel,
            recent_target_chars=1, current_input_id=self.current_input_id)
        self.coverage.append({'tool': 'search', 'query_sha256': hashlib.sha256(query.encode()).hexdigest(), 'query_chars': len(query),
            'candidates': len(page.hits), 'degraded': page.degraded,
            'lexical': asdict(page.lexical), 'semantic': asdict(page.semantic), 'at_limit': len(page.hits) == min(left, 10)})
        self.candidate_count += len(page.hits)
        for hit in page.hits:
            self.discovered.add(hit.evidence.message_id)
            self.admit(hit.evidence.message_id, max(0, hit.evidence.spans[0].start - 64) if hit.evidence.spans else 0)

    def navigation(self, message_id: str) -> None:
        self.runtime._require_scope(self.access, 'recall', 'reflection_navigation')
        if message_id not in self.discovered:
            raise MemoryFailure('reflection_source_not_discovered')
        left = self.candidates - self.candidate_count
        if left <= 0:
            raise MemoryFailure('reflection_candidate_budget')
        with self.runtime._connection(check=self.check) as db:
            db.execute('BEGIN')
            state = self.runtime._state(db, message_id)
            if state.status != 'active':
                raise MemoryFailure('reflection_source_unavailable')
            row = db.execute('SELECT event FROM messages WHERE id=?', (message_id,)).fetchone()
            origin = json.loads(row[0])['source']
            # Scope by exact discovered entry/session; not a caller-provided global export.
            eligible = _MEMORY_EVIDENCE.replace('messages.id', 'm.id')
            rows = db.execute(f'''SELECT m.id FROM messages m LEFT JOIN visibility v ON v.message_id=m.id
                WHERE coalesce(v.status,'active')='active' AND json_extract(m.event,'$.source.entry')=?
                AND json_extract(m.event,'$.source.session')=? AND json_extract(m.event,'$.completion')='complete'
                AND json_extract(m.event,'$.role') IN ('user','assistant','tool')
                AND json_extract(m.receipt,'$.original_saved')=1 AND json_extract(m.receipt,'$.representation')='original'
                AND coalesce(json_extract(m.event,'$.sensitive'),0)=0 AND coalesce(json_extract(m.event,'$.protected_source'),0)=0
                AND m.id IS NOT ?
                AND {eligible}
                ORDER BY abs(json_extract(m.event,'$.sequence')-?),m.id LIMIT ?''',
                (origin['entry'], origin['session'], self.current_input_id, json.loads(row[0])['sequence'], min(left, 8))).fetchall()
        self.coverage.append({'tool': 'navigation', 'candidates': len(rows), 'scope': 'discovered_session_neighbours', 'at_limit': len(rows) == min(left, 8)})
        self.candidate_count += len(rows)
        for item in rows:
            self.discovered.add(item[0])
            self.admit(item[0])

    def view(self, view_id: str) -> None:
        self.runtime._require_scope(self.access, 'recall', 'reflection_view')
        view = TopicViews(self.runtime, self.recall).read(self.access, view_id, check=self.check)
        self.coverage.append({'tool': 'view', 'view_id': view_id, 'state': view.state})
        if view.data is None:
            return
        self.views[view_id] = {'definition_revision': view.definition_revision, 'snapshot_revision': view.snapshot_revision, 'clock': view.clock}
        # Model sees original citations, not a generated conclusion as independent evidence.
        for citation in view.data['citations']:
            self.discovered.add(citation['message_id'])
            self.admit(citation['message_id'], citation['start'], min(2048, citation['end'] - citation['start']))

    def verify(self, check: Callable[[], float]) -> bool:
        self.runtime._require_scope(self.access, 'recall', 'reflection_handoff')
        unchanged = True
        valid = []
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            for item in self.evidence:
                check()
                span = EvidenceSpan(item['message_id'], item['state_version'], 'original', item['content_version'], item['start'], item['end'], item['quote'])
                try:
                    current = source_contexts(db, (span,), 'reflection_original')[0]
                    if current.get('transcription') != item.get('transcription'):
                        raise MemoryFailure('reflection_source_unavailable')
                    valid.append(item)
                except MemoryFailure:
                    unchanged = False
            for view_id, previous in self.views.items():
                view = TopicViews(self.runtime, self.recall)._read(db, view_id)
                if (view.state not in {'valid', 'conflict'} or view.definition_revision != previous['definition_revision']
                        or view.snapshot_revision != previous['snapshot_revision'] or view.clock != previous['clock']):
                    unchanged = False
        self.evidence = valid
        return unchanged
