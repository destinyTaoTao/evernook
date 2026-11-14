"""User-defined topic views: persistent clocks, coalesced jobs and fail-closed snapshots."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any

from .candidates import EvidenceSpan
from .models import Access, Event, MemoryFailure, Source
from .observations import ObservationRegistry, source_contexts
from .runtime import MemoryRuntime, _identifier, _json, _time
from .safety import credential_signal
from .semantic import SemanticRecall


TOPIC_VIEW_TABLES = ('topic_view_clock_v1', 'topic_view_definitions_v1', 'topic_view_requests_v1',
    'topic_view_snapshots_v1', 'topic_view_jobs_v1')


@dataclass(frozen=True)
class TopicViewDefinition:
    title: str
    purpose: str
    query: str
    entries: tuple[str, ...] = ()
    sessions: tuple[tuple[str, str], ...] = ()
    source_ids: tuple[str, ...] = ()
    observation_keys: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    ttl_seconds: int = 86400
    max_sources: int = 16
    max_chars: int = 8192
    policy_version: str = 'topic-view-v1'
    refresh_interval_seconds: int = 5


@dataclass(frozen=True)
class TopicView:
    view_id: str
    definition_revision: int
    snapshot_revision: int
    state: str
    data: dict[str, Any] | None
    definition: TopicViewDefinition | None
    clock: dict[str, Any] | None
    updated_at: str | None = None
    expires_at: str | None = None
    coverage: dict[str, Any] | None = None


class TopicViews:
    def __init__(self, runtime: MemoryRuntime, recall: SemanticRecall | None = None):
        self.runtime = runtime
        self.recall = recall or SemanticRecall(runtime)
        if self.recall.runtime is not runtime:
            raise MemoryFailure('invalid_topic_view_runtime')
        self._processor_identity: str | None = None

    @property
    def processor(self) -> str:
        if self._processor_identity is None:
            root = Path(__file__).parent
            digest = hashlib.sha256()
            # Bind the actual immutable installed implementation, including transitive
            # extraction/ranking rules. Paths and runtime/user data never enter the identity.
            for path in sorted((*root.rglob('*.py'), *root.rglob('*.mjs'))):
                digest.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
            digest.update((self.recall.space.key if self.recall.space else 'no-embedding').encode())
            self._processor_identity = 'topic-view-excerpts-v1:' + digest.hexdigest()
        return self._processor_identity

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS topic_view_clock_v1 (id INTEGER PRIMARY KEY CHECK(id=1),
            generation TEXT NOT NULL,evidence INTEGER NOT NULL,control INTEGER NOT NULL,derived INTEGER NOT NULL,last_source_id TEXT)''')
        db.execute('INSERT OR IGNORE INTO topic_view_clock_v1 VALUES (1,?,0,0,0,NULL)', (secrets.token_hex(16),))
        db.execute('''CREATE TABLE IF NOT EXISTS topic_view_definitions_v1 (view_id TEXT NOT NULL,revision INTEGER NOT NULL,
            state TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(view_id,revision))''')
        db.execute('''CREATE TABLE IF NOT EXISTS topic_view_requests_v1 (request_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,view_id TEXT NOT NULL,source_id TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS topic_view_snapshots_v1 (view_id TEXT NOT NULL,revision INTEGER NOT NULL,
            definition_revision INTEGER NOT NULL,processor TEXT NOT NULL,clock TEXT NOT NULL,created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,sources TEXT NOT NULL,observations TEXT NOT NULL,
            PRIMARY KEY(view_id,revision))''')
        db.execute('''CREATE TABLE IF NOT EXISTS topic_view_jobs_v1 (view_id TEXT PRIMARY KEY,state TEXT NOT NULL,
            target TEXT NOT NULL,token TEXT,lease_until REAL,attempt INTEGER NOT NULL,last_served REAL NOT NULL,error TEXT)''')
        ObservationRegistry._schema(db)
        from .topic_view_clock import install
        install(db)

    @staticmethod
    def _clock(db: sqlite3.Connection) -> dict[str, Any]:
        row = db.execute('SELECT generation,evidence,control,derived FROM topic_view_clock_v1 WHERE id=1').fetchone()
        if row is None:
            raise MemoryFailure('topic_view_clock_missing')
        return dict(row)

    def _clock_matches(self, db: sqlite3.Connection, clock: dict[str, Any], current_input_id: str | None) -> bool:
        actual = self._clock(db)
        if clock == actual:
            return True
        if (current_input_id is None or any(actual[name] != clock[name] for name in ('generation', 'control', 'derived'))
                or actual['evidence'] != clock['evidence'] + 1):
            return False
        last = db.execute('SELECT last_source_id FROM topic_view_clock_v1 WHERE id=1').fetchone()
        if not last or last[0] != current_input_id:
            return False
        stored = self.runtime._stored(db.execute('SELECT event,receipt FROM messages WHERE id=?', (current_input_id,)).fetchone())
        return (stored.event.role == 'user' and stored.event.completion == 'complete' and stored.receipt.original_saved
            and self.runtime._state(db, current_input_id).status == 'active')

    @staticmethod
    def _definition(value: dict[str, Any]) -> TopicViewDefinition:
        value = dict(value)
        for name in ('entries', 'sessions', 'source_ids', 'observation_keys'):
            value[name] = tuple(tuple(item) for item in value[name]) if name == 'sessions' else tuple(value[name])
        return TopicViewDefinition(**value)

    @staticmethod
    def _latest(db: sqlite3.Connection, view_id: str) -> dict[str, Any] | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_definitions_v1'").fetchone():
            return None
        row = db.execute('SELECT record FROM topic_view_definitions_v1 WHERE view_id=? ORDER BY revision DESC LIMIT 1', (view_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _validate(definition: TopicViewDefinition) -> None:
        if not isinstance(definition, TopicViewDefinition):
            raise MemoryFailure('invalid_topic_view_definition')
        for value, size in ((definition.title, 128), (definition.purpose, 512), (definition.query, 256)):
            if not isinstance(value, str) or not 0 < len(value.strip()) <= size or credential_signal(value):
                raise MemoryFailure('invalid_topic_view_definition')
        for name, maximum in (('entries', 16), ('sessions', 32), ('source_ids', 64), ('observation_keys', 8)):
            values = getattr(definition, name)
            if not isinstance(values, tuple) or len(values) > maximum:
                raise MemoryFailure('invalid_topic_view_definition')
            for value in values:
                if name == 'sessions':
                    if not isinstance(value, tuple) or len(value) != 2:
                        raise MemoryFailure('invalid_topic_view_scope')
                    for part in value:
                        _identifier(part)
                else:
                    _identifier(value)
        if (type(definition.ttl_seconds) is not int or not 1 <= definition.ttl_seconds <= 31536000
                or type(definition.refresh_interval_seconds) is not int or not 1 <= definition.refresh_interval_seconds <= 3600
                or type(definition.max_sources) is not int or not 1 <= definition.max_sources <= 32
                or type(definition.max_chars) is not int or not 512 <= definition.max_chars <= 16384):
            raise MemoryFailure('invalid_topic_view_budget')
        _identifier(definition.policy_version)
        for instant in (definition.since, definition.until):
            if instant is not None:
                _time(instant)
        if definition.since and definition.until and _time(definition.since) >= _time(definition.until):
            raise MemoryFailure('invalid_topic_view_scope')

    def _read(self, db: sqlite3.Connection, view_id: str, *, current_input_id: str | None = None) -> TopicView:
        record = self._latest(db, view_id)
        if record is None:
            raise MemoryFailure('topic_view_not_found')
        version = int(record['revision'])
        definition = self._definition(record['definition']) if record['definition'] else None
        if record['state'] in {'paused', 'deleted'}:
            return TopicView(view_id, version, 0, record['state'], None, definition, None)
        snapshot = db.execute('SELECT * FROM topic_view_snapshots_v1 WHERE view_id=? ORDER BY revision DESC LIMIT 1', (view_id,)).fetchone()
        if snapshot is None:
            return TopicView(view_id, version, 0, 'pending', None, definition, self._clock(db))
        clock = json.loads(snapshot['clock'])
        state = str(snapshot['state'])
        if snapshot['definition_revision'] != version or snapshot['processor'] != self.processor or not self._clock_matches(db, clock, current_input_id):
            state = 'needs_refresh'
        elif _time(snapshot['expires_at']) <= time.time():
            state = 'expired'
        else:
            try:
                spans = tuple(EvidenceSpan(**span) for span in json.loads(snapshot['sources']))
                fresh = source_contexts(db, spans, 'view_evidence')
                citations = json.loads(snapshot['data'])['citations']
                if len(citations) != len(fresh) or any(old.get('transcription') != current.get('transcription')
                        for old, current in zip(citations, fresh)):
                    raise MemoryFailure('stale_observation_source')
                for observation_id, revision in json.loads(snapshot['observations']):
                    observed = ObservationRegistry._latest(db, observation_id)
                    if observed is None or observed.revision != revision or ObservationRegistry._view(db, observed).state not in {'published', 'conflicted'}:
                        raise MemoryFailure('stale_observation_source')
                    if (observed.draft.valid_from is not None and _time(observed.draft.valid_from) > time.time()
                            or observed.draft.valid_until is not None and _time(observed.draft.valid_until) <= time.time()):
                        raise MemoryFailure('stale_observation_source')
            except MemoryFailure as error:
                if error.code not in {'message_not_found', 'stale_observation_source', 'observation_span_mismatch'}:
                    raise
                state = 'needs_refresh'
        current_data = json.loads(snapshot['data']) if state in {'valid', 'conflict', 'empty'} else None
        data = current_data if state in {'valid', 'conflict'} else None
        return TopicView(view_id, version, int(snapshot['revision']), state, data, definition, clock,
            str(snapshot['created_at']), str(snapshot['expires_at']), current_data['coverage'] if current_data else {'unavailable_state': state})

    @staticmethod
    def _request(db: sqlite3.Connection, request_id: str, fingerprint: str) -> bool:
        row = db.execute('SELECT fingerprint FROM topic_view_requests_v1 WHERE request_id=?', (request_id,)).fetchone()
        if row and row[0] != fingerprint:
            raise MemoryFailure('topic_view_request_conflict')
        return row is not None

    def _snapshot_state(self, db: sqlite3.Connection, record: dict[str, Any], processor: str, clock: dict[str, Any]) -> str:
        if record['state'] in {'paused', 'deleted'}:
            return str(record['state'])
        snapshot = db.execute('''SELECT definition_revision,processor,clock,expires_at,state FROM topic_view_snapshots_v1
            WHERE view_id=? ORDER BY revision DESC LIMIT 1''', (record['view_id'],)).fetchone()
        if snapshot is None:
            return 'pending'
        if snapshot['definition_revision'] != record['revision'] or snapshot['processor'] != processor or json.loads(snapshot['clock']) != clock:
            return 'needs_refresh'
        if _time(snapshot['expires_at']) <= time.time():
            return 'expired'
        return str(snapshot['state'])

    def _write_definition(self, db: sqlite3.Connection, request_id: str, fingerprint: str, view_id: str,
                          revision: int, state: str, definition: TopicViewDefinition | None, edits: tuple[str, ...] = (),
                          source_id: str | None = None) -> None:
        record = {'view_id': view_id, 'revision': revision, 'state': state,
            'definition': asdict(definition) if definition else None, 'edit_source_ids': edits}
        db.execute('INSERT INTO topic_view_definitions_v1 VALUES (?,?,?,?)', (view_id, revision, state, _json(record)))
        db.execute('INSERT INTO topic_view_requests_v1 VALUES (?,?,?,?)', (request_id, fingerprint, view_id, source_id))
        if state == 'active':
            target = _json([revision, self._clock(db)])
            db.execute('''INSERT INTO topic_view_jobs_v1 VALUES (?,'pending',?,NULL,NULL,0,0,NULL)
                ON CONFLICT(view_id) DO UPDATE SET state='pending',target=excluded.target,token=NULL,lease_until=NULL,error=NULL''', (view_id, target))

    def configure(self, access: Access, request_id: str, view_id: str, expected_revision: int,
                  definition: TopicViewDefinition) -> TopicView:
        self.runtime._require_scope(access, 'recall', 'topic_view_configure')
        self.runtime._require_scope(access, 'maintain', 'topic_view_configure')
        self.runtime._require_scope(access, 'accept', 'topic_view_configure')
        for value in (request_id, view_id):
            _identifier(value)
        self._validate(definition)
        if type(expected_revision) is not int or expected_revision < 0:
            raise MemoryFailure('invalid_topic_view_revision')
        fingerprint = hashlib.sha256(_json(['define', view_id, expected_revision, asdict(definition)]).encode()).hexdigest()
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            if self._request(db, request_id, fingerprint):
                return self._read(db, view_id)
            previous = self._latest(db, view_id)
            if expected_revision != (int(previous['revision']) if previous else 0) or previous and previous['state'] == 'deleted':
                raise MemoryFailure('topic_view_revision_conflict')
            if previous is None and db.execute('''SELECT count(*) FROM topic_view_definitions_v1 d WHERE d.state<>'deleted'
                AND NOT EXISTS (SELECT 1 FROM topic_view_definitions_v1 n WHERE n.view_id=d.view_id AND n.revision>d.revision)''').fetchone()[0] >= 128:
                raise MemoryFailure('topic_view_definition_limit')
            state = 'paused' if previous and previous['state'] == 'paused' else 'active'
            self._write_definition(db, request_id, fingerprint, view_id, expected_revision + 1, state, definition,
                tuple(previous['edit_source_ids']) if previous else ())
            result = self._read(db, view_id)
            db.execute('COMMIT')
            return result

    def control(self, access: Access, request_id: str, view_id: str, expected_revision: int, *, action: str) -> TopicView:
        self.runtime._require_scope(access, 'recall', 'topic_view_control')
        self.runtime._require_scope(access, 'maintain', 'topic_view_control')
        self.runtime._require_scope(access, 'accept', 'topic_view_control')
        for value in (request_id, view_id):
            _identifier(value)
        if action not in {'pause', 'resume', 'delete'} or type(expected_revision) is not int:
            raise MemoryFailure('invalid_topic_view_control')
        fingerprint = hashlib.sha256(_json(['control', view_id, expected_revision, action]).encode()).hexdigest()
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            if self._request(db, request_id, fingerprint):
                return self._read(db, view_id)
            previous = self._latest(db, view_id)
            if not previous or previous['revision'] != expected_revision or previous['state'] == 'deleted':
                raise MemoryFailure('topic_view_revision_conflict')
            state = {'pause': 'paused', 'resume': 'active', 'delete': 'deleted'}[action]
            definition = self._definition(previous['definition']) if state != 'deleted' else None
            if action == 'delete':
                db.execute('DELETE FROM topic_view_snapshots_v1 WHERE view_id=?', (view_id,))
                db.execute('DELETE FROM topic_view_definitions_v1 WHERE view_id=?', (view_id,))
            if state == 'deleted':
                db.execute('DELETE FROM topic_view_jobs_v1 WHERE view_id=?', (view_id,))
            elif state == 'paused':
                db.execute("UPDATE topic_view_jobs_v1 SET state='paused',token=NULL,lease_until=NULL WHERE view_id=?", (view_id,))
            self._write_definition(db, request_id, fingerprint, view_id, expected_revision + 1, state, definition,
                tuple(previous['edit_source_ids']) if state != 'deleted' else ())
            result = self._read(db, view_id)
            db.execute('COMMIT')
            return result

    def read(self, access: Access, view_id: str, *, check: Callable[[], float] | None = None,
             current_input_id: str | None = None) -> TopicView:
        self.runtime._require_scope(access, 'recall', 'topic_view_read')
        _identifier(view_id)
        if current_input_id is not None:
            _identifier(current_input_id)
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            return self._read(db, view_id, current_input_id=current_input_id)

    def add_evidence(self, access: Access, request_id: str, view_id: str, expected_revision: int, text: str) -> dict[str, Any]:
        self.runtime._require_scope(access, 'recall', 'topic_view_edit')
        self.runtime._require_scope(access, 'maintain', 'topic_view_edit')
        self.runtime._require_scope(access, 'accept', 'topic_view_edit')
        identity = self.runtime._require_scope(access, 'capture', 'topic_view_edit')
        for value in (request_id, view_id):
            _identifier(value)
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 8192 or credential_signal(text) or type(expected_revision) is not int:
            raise MemoryFailure('invalid_topic_view_edit')
        fingerprint = hashlib.sha256(_json(['edit', view_id, expected_revision, text]).encode()).hexdigest()
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            previous = self._latest(db, view_id)
            if self._request(db, request_id, fingerprint):
                saved = db.execute('SELECT source_id FROM topic_view_requests_v1 WHERE request_id=?', (request_id,)).fetchone()
                receipt = db.execute('SELECT receipt FROM messages WHERE id=?', (saved['source_id'],)).fetchone() if saved else None
                return {'view': self._read(db, view_id), 'replayed': True,
                    'source_receipt': json.loads(receipt[0]) if receipt else None}
            if not previous or previous['revision'] != expected_revision or previous['state'] != 'active':
                raise MemoryFailure('topic_view_revision_conflict')
            if len(previous['edit_source_ids']) >= 64:
                raise MemoryFailure('topic_view_edit_limit')
            event = Event(Source(identity.entry, 'topic-view:' + hashlib.sha256(view_id.encode()).hexdigest()[:32], 'user-edits', request_id),
                'user', expected_revision + 1, datetime.now(timezone.utc).isoformat(), 'complete', text)
            self.runtime._validate_event(event)
            event_fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(event)).encode(), hashlib.sha256)
            # Raw evidence, its clock, revision and refresh request share one transaction.
            receipt = self.runtime._persist_event(db, event, event, event_fingerprint)
            self._write_definition(db, request_id, fingerprint, view_id, expected_revision + 1, 'active',
                self._definition(previous['definition']), (*previous['edit_source_ids'], receipt.message_id), source_id=receipt.message_id)
            result = {'view': self._read(db, view_id), 'source_receipt': asdict(receipt), 'replayed': False}
            db.execute('COMMIT')
            return result

    def status(self, access: Access) -> dict[str, Any]:
        self.runtime._require_scope(access, 'maintain', 'topic_view_status')
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_definitions_v1'").fetchone():
                return {'definitions': 0, 'stored_jobs': 0, 'clock': None}
            rows = db.execute('''SELECT d.view_id FROM topic_view_definitions_v1 d WHERE NOT EXISTS (
                SELECT 1 FROM topic_view_definitions_v1 newer WHERE newer.view_id=d.view_id AND newer.revision>d.revision)''').fetchall()
            clock, processor = self._clock(db), self.processor
            return {'definitions': len(rows), 'stored_jobs': db.execute('SELECT count(*) FROM topic_view_jobs_v1').fetchone()[0],
                'clock': clock, 'views': [{'view_id': row[0], 'state': self._snapshot_state(db, self._latest(db, row[0]) or {}, processor, clock)} for row in rows]}

    def refresh(self, access: Access, view_id: str, *, timeout: float = 10, cancel: threading.Event | None = None) -> TopicView:
        from .topic_view_refresh import refresh
        return refresh(self, access, view_id, timeout=timeout, cancel=cancel)

    def tick(self, access: Access, *, timeout: float = 5, cancel: threading.Event | None = None) -> dict[str, Any]:
        from .topic_view_refresh import view_budget
        self.runtime._require_scope(access, 'maintain', 'topic_view_tick')
        remaining = view_budget(timeout, cancel)
        remaining()
        with self.runtime._connection(check=remaining) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_definitions_v1'").fetchone():
                return {'refreshed': 0, 'state': 'unconfigured'}
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT d.view_id,d.revision,d.record FROM topic_view_definitions_v1 d WHERE d.state='active'
                AND NOT EXISTS (SELECT 1 FROM topic_view_definitions_v1 newer WHERE newer.view_id=d.view_id AND newer.revision>d.revision)''').fetchall()
            clock, processor = self._clock(db), self.processor
            for row in rows:
                remaining()
                if self._snapshot_state(db, json.loads(row['record']), processor, clock) in {'valid', 'conflict', 'empty'}:
                    continue
                target = _json([row['revision'], clock])
                db.execute('''INSERT INTO topic_view_jobs_v1 VALUES (?,'pending',?,NULL,NULL,0,0,NULL)
                    ON CONFLICT(view_id) DO UPDATE SET target=excluded.target,
                    state=CASE WHEN topic_view_jobs_v1.state='running' AND topic_view_jobs_v1.lease_until>? THEN 'running' ELSE 'pending' END''',
                    (row[0], target, time.time()))
            job = db.execute('''SELECT j.view_id FROM topic_view_jobs_v1 j JOIN topic_view_definitions_v1 d ON d.view_id=j.view_id
                WHERE d.state='active' AND NOT EXISTS (SELECT 1 FROM topic_view_definitions_v1 n WHERE n.view_id=d.view_id AND n.revision>d.revision)
                AND (j.state='pending' OR j.state='running' AND j.lease_until<=?)
                AND j.last_served+json_extract(d.record,'$.definition.refresh_interval_seconds')<=?
                ORDER BY j.last_served,j.view_id LIMIT 1''', (time.time(), time.time())).fetchone()
            db.execute('COMMIT')
        if not job:
            return {'refreshed': 0, 'state': 'idle'}
        result = self.refresh(access, job['view_id'], timeout=remaining(), cancel=cancel)
        return {'refreshed': int(result.state in {'valid', 'conflict', 'empty'}), 'state': result.state, 'view_id': result.view_id}


def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_snapshots_v1'").fetchone():
        return
    affected, targets = set(), set(ids)
    for row in db.execute('SELECT * FROM topic_view_snapshots_v1'):
        references = {span['message_id'] for span in json.loads(row['sources'])}
        missing_observation = any(ObservationRegistry._latest(db, item[0]) is None for item in json.loads(row['observations']))
        if targets.intersection(references) or missing_observation:
            affected.add(str(row['view_id']))
    for view_id in affected:
        db.execute('DELETE FROM topic_view_snapshots_v1 WHERE view_id=?', (view_id,))
        db.execute("UPDATE topic_view_jobs_v1 SET state='pending',token=NULL,lease_until=NULL WHERE view_id=? AND state<>'paused'", (view_id,))
