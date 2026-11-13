"""Versioned derived observations with raw evidence, counterevidence and explicit authority."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
from typing import Any

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .profile import ProfileRegistry
from .runtime import MemoryRuntime, _identifier, _json, _time
from .safety import credential_signal


OBSERVATION_TABLES = ('observation_events_v1', 'observation_requests_v1', 'observation_policy_v1',
    'observation_progress_v1', 'observation_atoms_v1', 'observation_runs_v1', 'observation_blocked_v1')


@dataclass(frozen=True)
class ObservationDraft:
    topic_key: str
    conclusion: str = field(repr=False)
    support: tuple[EvidenceSpan, ...]
    counterevidence: tuple[EvidenceSpan, ...]
    category: str
    valid_from: str | None
    valid_until: str | None
    processor_version: str
    policy_version: str
    inference: bool = True
    disputed: bool = False


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    revision: int
    state: str
    operation: str
    draft: ObservationDraft
    recorded_at: str
    parents: tuple[str, ...] = ()
    confirmation_id: str | None = None
    confirmation_version: int | None = None
    source_remap: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ObservationView:
    observation_id: str
    revision: int
    state: str
    conclusion: str | None
    independent_support: int
    independent_counterevidence: int
    source_contexts: tuple[dict[str, Any], ...]
    record: ObservationRecord | None
    inference: bool = True


@dataclass(frozen=True)
class ObservationPage:
    rows: tuple[ObservationView, ...]
    next_offset: int | None
    coverage: str = 'structured_slot_and_literal_only'


def source_contexts(db: sqlite3.Connection, spans: tuple[EvidenceSpan, ...], stance: str) -> tuple[dict[str, Any], ...]:
    contexts = []
    for span in spans:
        if (not isinstance(span, EvidenceSpan) or span.representation != 'original'
                or any(type(value) is not int for value in (span.start, span.end, span.state_version, span.content_version))
                or span.start < 0 or span.end <= span.start or span.state_version < 1 or span.content_version < 1):
            raise MemoryFailure('invalid_observation_span')
        from .runtime import _MEMORY_EVIDENCE
        if not db.execute(f'SELECT 1 FROM messages WHERE id=? AND {_MEMORY_EVIDENCE}', (span.message_id,)).fetchone():
            raise MemoryFailure('stale_observation_source')
        state = MemoryRuntime._state(db, span.message_id)
        stored = MemoryRuntime._stored(db.execute('SELECT event,receipt FROM messages WHERE id=?', (span.message_id,)).fetchone())
        event, receipt = stored.event, stored.receipt
        if (state.status != 'active' or state.version != span.state_version
                or receipt.content_version != span.content_version or not receipt.original_saved
                or receipt.representation != 'original' or event.completion != 'complete'
                or event.role not in {'user', 'assistant', 'tool'} or event.sensitive or event.protected_source):
            raise MemoryFailure('stale_observation_source')
        if span.end > len(event.text) or event.text[span.start:span.end] != span.expected_text:
            raise MemoryFailure('observation_span_mismatch')
        contexts.append({'message_id': span.message_id, 'state_version': span.state_version,
            'content_version': span.content_version, 'start': span.start, 'end': span.end,
            'quote': span.expected_text, 'stance': stance, 'role': event.role, 'origin': asdict(event.source),
            'occurred_at': event.occurred_at, 'learned_at': receipt.received_at,
            # Assistant/tool material can be useful context, but cannot self-reinforce user patterns.
            'independent_user_evidence': event.role == 'user'})
    return tuple(contexts)


class ObservationRegistry:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS observation_events_v1 (
            observation_id TEXT NOT NULL, revision INTEGER NOT NULL, topic_key TEXT NOT NULL,
            state TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(observation_id,revision))''')
        db.execute('CREATE INDEX IF NOT EXISTS observation_topics_v1 ON observation_events_v1(topic_key,observation_id,revision)')
        db.execute('''CREATE TABLE IF NOT EXISTS observation_requests_v1 (
            request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, observation_id TEXT NOT NULL, revision INTEGER NOT NULL)''')

    @staticmethod
    def _record(raw: str) -> ObservationRecord:
        value = json.loads(raw)
        value['parents'] = tuple(value['parents'])
        value['source_remap'] = tuple(tuple(pair) for pair in value.get('source_remap', ()))
        for name in ('support', 'counterevidence'):
            value['draft'][name] = tuple(EvidenceSpan(**item) for item in value['draft'][name])
        value['draft'] = ObservationDraft(**value['draft'])
        return ObservationRecord(**value)

    @staticmethod
    def _latest(db: sqlite3.Connection, observation_id: str) -> ObservationRecord | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_events_v1'").fetchone():
            return None
        row = db.execute('SELECT record FROM observation_events_v1 WHERE observation_id=? ORDER BY revision DESC LIMIT 1',
                         (observation_id,)).fetchone()
        return ObservationRegistry._record(row[0]) if row else None

    @staticmethod
    def _canonical(draft: ObservationDraft) -> ObservationDraft:
        if (not isinstance(draft, ObservationDraft) or type(draft.inference) is not bool or not draft.inference or type(draft.disputed) is not bool
                or not isinstance(draft.conclusion, str) or not 1 <= len(draft.conclusion.strip()) <= 2048
                or credential_signal(draft.conclusion) or not isinstance(draft.support, tuple)
                or not isinstance(draft.counterevidence, tuple) or not 1 <= len(draft.support) + len(draft.counterevidence) <= 64):
            raise MemoryFailure('invalid_observation')
        if any(not isinstance(span, EvidenceSpan) or not isinstance(span.expected_text, str)
               for span in (*draft.support, *draft.counterevidence)):
            raise MemoryFailure('invalid_observation_span')
        if sum(len(span.expected_text) for span in (*draft.support, *draft.counterevidence)) > 16384:
            raise MemoryFailure('observation_evidence_budget')
        for span in (*draft.support, *draft.counterevidence):
            _identifier(span.message_id)
        for key in (draft.topic_key, draft.category, draft.processor_version, draft.policy_version):
            _identifier(key)
        for value in (draft.valid_from, draft.valid_until):
            if value is not None:
                _time(value)
        if draft.valid_from is not None and draft.valid_until is not None and _time(draft.valid_until) <= _time(draft.valid_from):
            raise MemoryFailure('invalid_observation_time')
        return replace(draft, support=tuple(dict.fromkeys(draft.support)), counterevidence=tuple(dict.fromkeys(draft.counterevidence)))

    def _id(self, topic_key: str, parents: tuple[str, ...]) -> str:
        return 'observation:' + hmac.digest(self.runtime._fingerprint_key, _json([topic_key, parents]).encode(), hashlib.sha256).hex()

    @staticmethod
    def _append(db: sqlite3.Connection, record: ObservationRecord, request_id: str, fingerprint: str) -> None:
        db.execute('INSERT INTO observation_events_v1 VALUES (?,?,?,?,?)',
            (record.observation_id, record.revision, record.draft.topic_key, record.state, _json(asdict(record))))
        db.execute('INSERT INTO observation_requests_v1 VALUES (?,?,?,?)',
            (request_id, fingerprint, record.observation_id, record.revision))

    @staticmethod
    def _request(db: sqlite3.Connection, request_id: str, fingerprint: str) -> str | None:
        row = db.execute('SELECT fingerprint,observation_id FROM observation_requests_v1 WHERE request_id=?', (request_id,)).fetchone()
        if row and not hmac.compare_digest(row[0], fingerprint):
            raise MemoryFailure('observation_request_conflict')
        return str(row[1]) if row else None

    @staticmethod
    def _contexts(db: sqlite3.Connection, draft: ObservationDraft) -> tuple[dict[str, Any], ...]:
        return (*source_contexts(db, draft.support, 'support'), *source_contexts(db, draft.counterevidence, 'counterevidence'))

    @staticmethod
    def _view(db: sqlite3.Connection, record: ObservationRecord) -> ObservationView:
        if record.state in {'withdrawn', 'needs_review'}:
            return ObservationView(record.observation_id, record.revision, record.state, None, 0, 0, (), None)
        try:
            contexts = ObservationRegistry._contexts(db, record.draft)
            if record.confirmation_id is not None and ProfileRegistry._confirmation(db, record.confirmation_id) != record.confirmation_version:
                raise MemoryFailure('stale_observation_source')
        except MemoryFailure as error:
            if error.code not in {'stale_observation_source', 'message_not_found', 'observation_span_mismatch', 'profile_user_confirmation_required'}:
                raise
            return ObservationView(record.observation_id, record.revision, 'unavailable', None, 0, 0, (), None)
        counts = {stance: len({item['message_id'] for item in contexts if item['stance'] == stance and item['independent_user_evidence']})
                  for stance in ('support', 'counterevidence')}
        return ObservationView(record.observation_id, record.revision, record.state, record.draft.conclusion,
            counts['support'], counts['counterevidence'], contexts, record)

    def propose(self, access: Access, request_id: str, draft: ObservationDraft, *, expected_revision: int = 0,
                operation: str = 'create', observation_id: str | None = None, parents: tuple[str, ...] = (),
                source_remap: tuple[tuple[str, str], ...] = (),
                check: Callable[[], float] | None = None) -> ObservationView:
        self.runtime._require_scope(access, 'maintain', 'observation_propose')
        _identifier(request_id)
        draft = self._canonical(draft)
        if (operation not in {'create', 'merge', 'narrow', 'change', 'correction'}
                or type(expected_revision) is not int or expected_revision < 0 or not isinstance(parents, tuple) or len(parents) > 16):
            raise MemoryFailure('invalid_observation_operation')
        for parent in parents:
            _identifier(parent)
        if (not isinstance(source_remap, tuple) or len(source_remap) > 64
                or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in source_remap)):
            raise MemoryFailure('invalid_observation_remap')
        for pair in source_remap:
            for value in pair:
                _identifier(value)
        if source_remap and {pair[1] for pair in source_remap} != {span.message_id for span in (*draft.support, *draft.counterevidence)}:
            raise MemoryFailure('invalid_observation_remap')
        selected = observation_id or self._id(draft.topic_key, parents)
        _identifier(selected)
        fingerprint = hashlib.sha256(_json([asdict(draft), expected_revision, operation, selected, parents, source_remap]).encode()).hexdigest()
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            repeated = self._request(db, request_id, fingerprint)
            previous = self._latest(db, selected)
            if repeated:
                assert previous is not None
                return self._view(db, previous)
            if expected_revision != (previous.revision if previous else 0) or (previous is None) != (operation == 'create'):
                raise MemoryFailure('observation_revision_conflict')
            if previous:
                if previous.state == 'withdrawn' or previous.draft.topic_key != draft.topic_key:
                    raise MemoryFailure('observation_requires_new_identity')
                if operation in {'merge', 'narrow'} and (not set(previous.draft.support).issubset(set(draft.support))
                        or not set(previous.draft.counterevidence).issubset(set(draft.counterevidence))):
                    raise MemoryFailure('observation_evidence_loss')
            self._contexts(db, draft)
            record = ObservationRecord(selected, expected_revision + 1, 'candidate', operation, draft,
                datetime.now(timezone.utc).isoformat(), parents, source_remap=source_remap)
            self._append(db, record, request_id, fingerprint)
            if check:
                check()
            result = self._view(db, record)
            db.execute('COMMIT')
            return result

    def rebind(self, access: Access, request_id: str, draft: ObservationDraft, source_remap: dict[str, str], *,
               check: Callable[[], float] | None = None) -> ObservationView:
        """Explicit import mapping never inherits publication, old versions or processor admission."""
        self.runtime._require_scope(access, 'maintain', 'observation_rebind')
        original = self._canonical(draft)
        if (not isinstance(source_remap, dict) or set(source_remap) != {span.message_id for span in (*original.support, *original.counterevidence)}
                or len(set(source_remap.values())) != len(source_remap)):
            raise MemoryFailure('invalid_observation_remap')
        for old, new in source_remap.items():
            _identifier(old)
            _identifier(new)
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            def rebound(spans: tuple[EvidenceSpan, ...]) -> tuple[EvidenceSpan, ...]:
                result = []
                for span in spans:
                    message_id = source_remap[span.message_id]
                    state = MemoryRuntime._state(db, message_id)
                    stored = MemoryRuntime._stored(db.execute('SELECT event,receipt FROM messages WHERE id=?', (message_id,)).fetchone())
                    result.append(replace(span, message_id=message_id, state_version=state.version, content_version=stored.receipt.content_version))
                return tuple(result)
            imported = replace(original, support=rebound(original.support), counterevidence=rebound(original.counterevidence),
                processor_version='rebound:' + hashlib.sha256(original.processor_version.encode()).hexdigest(), policy_version='rebind-review-v1')
            self._contexts(db, imported)
        parent = 'imported:' + hashlib.sha256(_json(asdict(original)).encode()).hexdigest()
        return self.propose(access, request_id, imported, parents=(parent,), source_remap=tuple(sorted(source_remap.items())), check=check)


    def decide(self, access: Access, request_id: str, observation_id: str, expected_revision: int, *, action: str,
               confirmation_id: str, check: Callable[[], float] | None = None) -> ObservationView:
        self.runtime._require_scope(access, 'accept', 'observation_decide')
        self.runtime._require_scope(access, 'maintain', 'observation_decide')
        for value in (request_id, observation_id, confirmation_id):
            _identifier(value)
        if action not in {'publish', 'withdraw'} or type(expected_revision) is not int or expected_revision < 1:
            raise MemoryFailure('invalid_observation_decision')
        fingerprint = hashlib.sha256(_json([observation_id, expected_revision, action, confirmation_id]).encode()).hexdigest()
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            repeated = self._request(db, request_id, fingerprint)
            previous = self._latest(db, observation_id)
            if previous is None:
                raise MemoryFailure('observation_not_found')
            if repeated:
                return self._view(db, previous)
            if previous.revision != expected_revision or previous.state == 'withdrawn':
                raise MemoryFailure('observation_revision_conflict')
            if action == 'publish' and previous.state == 'needs_review':
                raise MemoryFailure('observation_requires_split_review')
            confirmation_version = ProfileRegistry._confirmation(db, confirmation_id)
            if action == 'publish':
                self._contexts(db, previous.draft)
            state = 'withdrawn' if action == 'withdraw' else 'conflicted' if previous.draft.counterevidence or previous.draft.disputed else 'published'
            record = replace(previous, revision=previous.revision + 1, state=state,
                recorded_at=datetime.now(timezone.utc).isoformat(), confirmation_id=confirmation_id,
                confirmation_version=confirmation_version)
            self._append(db, record, request_id, fingerprint)
            if check:
                check()
            result = self._view(db, record)
            db.execute('COMMIT')
            return result

    def read(self, access: Access, observation_id: str, *, check: Callable[[], float] | None = None) -> ObservationView:
        self.runtime._require_scope(access, 'recall', 'observation_read')
        self.runtime._require_scope(access, 'maintain', 'observation_read')
        _identifier(observation_id)
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            record = self._latest(db, observation_id)
            if record is None:
                raise MemoryFailure('observation_not_found')
            return self._view(db, record)

    def related(self, access: Access, topic_key: str, *, query: str = '', limit: int = 16, offset: int = 0,
                check: Callable[[], float] | None = None) -> ObservationPage:
        """Maintenance lookup independent of QA RRF: exact slot first, then literal overlap."""
        self.runtime._require_scope(access, 'maintain', 'observation_related')
        _identifier(topic_key)
        return self._lookup(topic_key, query, limit, offset, published_only=False, check=check)

    def query(self, access: Access, topic_key: str, *, limit: int = 16, offset: int = 0,
              check: Callable[[], float] | None = None) -> ObservationPage:
        self.runtime._require_scope(access, 'recall', 'observation_query')
        _identifier(topic_key)
        return self._lookup(topic_key, '', limit, offset, published_only=True, check=check)

    def _lookup(self, topic_key: str, query: str, limit: int, offset: int, *, published_only: bool,
                check: Callable[[], float] | None) -> ObservationPage:
        if (not isinstance(query, str) or len(query) > 256 or credential_signal(query)
                or type(limit) is not int or not 1 <= limit <= 32 or type(offset) is not int or offset < 0):
            raise MemoryFailure('invalid_observation_query')
        tokens = list(dict.fromkeys(re.findall(r'[A-Za-z0-9_-]+|[\u4e00-\u9fff]{2}', query)))[:32]
        overlap = '+'.join('CASE WHEN instr(json_extract(e.record,\'$.draft.conclusion\'),?)>0 THEN 1 ELSE 0 END' for _ in tokens) or '0'
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_events_v1'").fetchone():
                return ObservationPage((), None)
            # No fixed prefix scan or QA channel votes can crowd out a structured slot.
            rows = db.execute(f'''SELECT e.record,({overlap}) AS overlap FROM observation_events_v1 e
                WHERE NOT EXISTS (SELECT 1 FROM observation_events_v1 newer WHERE newer.observation_id=e.observation_id
                    AND newer.revision>e.revision) AND e.state<> 'withdrawn'
                AND (e.topic_key=? OR ({overlap})>0)
                ORDER BY (e.topic_key=?) DESC,overlap DESC,e.observation_id LIMIT ? OFFSET ?''',
                (*tokens, topic_key, *tokens, topic_key, limit + 1, offset)).fetchall()
            result = []
            now = datetime.now(timezone.utc).timestamp()
            for row in rows[:limit]:
                record = self._record(row['record'])
                if published_only and (record.state not in {'published', 'conflicted'}
                        or record.draft.valid_from is not None and _time(record.draft.valid_from) > now
                        or record.draft.valid_until is not None and _time(record.draft.valid_until) <= now):
                    continue
                view = self._view(db, record)
                if not published_only or view.state != 'unavailable':
                    result.append(view)
            if check:
                check()
            return ObservationPage(tuple(result), offset + limit if len(rows) > limit else None)

    def split(self, access: Access, request_id: str, observation_id: str, expected_revision: int,
              drafts: tuple[ObservationDraft, ...], *, confirmation_id: str,
              check: Callable[[], float] | None = None) -> tuple[ObservationView, ...]:
        """Atomically withdraw a parent and create reviewed children retaining all its evidence."""
        self.runtime._require_scope(access, 'maintain', 'observation_split')
        self.runtime._require_scope(access, 'accept', 'observation_split')
        for value in (request_id, observation_id, confirmation_id):
            _identifier(value)
        if not isinstance(drafts, tuple) or not 2 <= len(drafts) <= 8 or type(expected_revision) is not int:
            raise MemoryFailure('invalid_observation_split')
        normalized = tuple(self._canonical(draft) for draft in drafts)
        if len({draft.topic_key for draft in normalized}) != len(normalized):
            raise MemoryFailure('invalid_observation_split')
        fingerprint = hashlib.sha256(_json([observation_id, expected_revision, [asdict(draft) for draft in normalized], confirmation_id]).encode()).hexdigest()
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            repeated = self._request(db, request_id, fingerprint)
            parent = self._latest(db, observation_id)
            if parent is None:
                raise MemoryFailure('observation_not_found')
            children = tuple(self._id(draft.topic_key, (observation_id,)) for draft in normalized)
            if repeated:
                records = tuple(self._latest(db, child) for child in children)
                if any(record is None for record in records):
                    raise MemoryFailure('observation_split_incomplete')
                return tuple(self._view(db, record) for record in records if record is not None)
            if parent.revision != expected_revision or parent.state == 'withdrawn':
                raise MemoryFailure('observation_revision_conflict')
            version = ProfileRegistry._confirmation(db, confirmation_id)
            required_support, required_counters = set(parent.draft.support), set(parent.draft.counterevidence)
            if parent.state == 'needs_review' and db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_atoms_v1'").fetchone():
                for atom in db.execute('SELECT DISTINCT span,stance FROM observation_atoms_v1 WHERE topic_key=?', (parent.draft.topic_key,)):
                    span = EvidenceSpan(**json.loads(atom['span']))
                    try:
                        source_contexts(db, (span,), atom['stance'])
                    except MemoryFailure as failure:
                        if failure.code in {'stale_observation_source', 'message_not_found', 'observation_span_mismatch'}:
                            continue
                        raise
                    (required_support if atom['stance'] == 'support' else required_counters).add(span)
                    if len(required_support) + len(required_counters) > 512:
                        raise MemoryFailure('observation_split_evidence_limit')
            if (not required_support.issubset({span for draft in normalized for span in draft.support})
                    or not required_counters.issubset({span for draft in normalized for span in draft.counterevidence})):
                raise MemoryFailure('observation_evidence_loss')
            now = datetime.now(timezone.utc).isoformat()
            views = []
            for index, (child, draft) in enumerate(zip(children, normalized)):
                if self._latest(db, child) is not None:
                    raise MemoryFailure('observation_revision_conflict')
                self._contexts(db, draft)
                record = ObservationRecord(child, 1, 'candidate', 'split', draft, now, (observation_id,))
                self._append(db, record, request_id + ':' + str(index), fingerprint)
                views.append(self._view(db, record))
            self._append(db, replace(parent, revision=parent.revision + 1, state='withdrawn',
                operation='split', recorded_at=now, confirmation_id=confirmation_id, confirmation_version=version), request_id, fingerprint)
            if check:
                check()
            db.execute('COMMIT')
            return tuple(views)

def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    """Purge all versions of affected observations and source-bearing maintenance artifacts."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_events_v1'").fetchone():
        return
    targets, affected = set(ids), set()
    for row in db.execute('SELECT observation_id,record FROM observation_events_v1'):
        record = ObservationRegistry._record(row['record'])
        if (targets.intersection(span.message_id for span in (*record.draft.support, *record.draft.counterevidence))
                or record.confirmation_id in targets):
            affected.add(record.observation_id)
    for observation_id in affected:
        db.execute('DELETE FROM observation_events_v1 WHERE observation_id=?', (observation_id,))
        db.execute('DELETE FROM observation_requests_v1 WHERE observation_id=?', (observation_id,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_runs_v1'").fetchone():
        runs: set[str] = set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_runs_v1'").fetchone():
            runs.update(str(row['id']) for row in db.execute('SELECT id,sources FROM candidate_runs_v1')
                        if targets.intersection(item[0] for item in json.loads(row['sources'])))
        runs.update(str(row['run_id']) for row in db.execute('SELECT * FROM observation_runs_v1')
                    if affected.intersection(json.loads(row['observation_ids'])))
        # Surviving sources need a retryable candidate lease as well as cleared progress.
        # Leave any unrelated observation request receipts intact; the worker reuses them.
        for run in runs:
            db.execute('DELETE FROM fact_candidates_v1 WHERE run_id=?', (run,))
            db.execute("UPDATE candidate_runs_v1 SET state='failed',error_code='dependency_unavailable',result_hash=NULL,lease_until=NULL WHERE id=?", (run,))
        db.executemany('DELETE FROM observation_runs_v1 WHERE run_id=?', ((run,) for run in runs))
        db.executemany('DELETE FROM observation_progress_v1 WHERE run_id=?', ((run,) for run in runs))
        db.executemany('DELETE FROM observation_progress_v1 WHERE message_id=?', ((item,) for item in ids))
        db.executemany('DELETE FROM observation_atoms_v1 WHERE message_id=?', ((item,) for item in ids))

