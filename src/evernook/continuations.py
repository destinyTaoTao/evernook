"""Source-backed work checkpoints; native summaries remain untrusted evidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json, _time


@dataclass(frozen=True)
class ContinuationDraft:
    task_id: str
    branch_id: str
    expected_revision: int
    anchor_id: str
    goal: EvidenceSpan
    progress: tuple[EvidenceSpan, ...]
    pending: tuple[EvidenceSpan, ...]
    state: str
    expires_at: str
    dependencies: tuple[tuple[str, int, int], ...] = ()
    consumed_checkpoints: tuple[str, ...] = ()
    goal_role: str = 'explicit_task_goal'


@dataclass(frozen=True)
class ContinuationView:
    checkpoint_id: str
    task_id: str
    branch_id: str
    revision: int
    state: str
    expires_at: str
    goal: dict[str, Any]
    progress: tuple[dict[str, Any], ...]
    pending: tuple[dict[str, Any], ...]
    request_id: str
    goal_role: str = 'explicit_task_goal'
    omitted_sources: tuple[str, ...] = ()


class Continuations:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS continuation_checkpoints_v1 (
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint BLOB NOT NULL,
            task_id TEXT NOT NULL,branch_id TEXT NOT NULL,revision INTEGER NOT NULL,record TEXT NOT NULL,
            UNIQUE(owner,request_id),UNIQUE(task_id,branch_id,revision))''')

    @staticmethod
    def _validate(draft: ContinuationDraft) -> None:
        if (not isinstance(draft, ContinuationDraft) or type(draft.expected_revision) is not int
                or draft.expected_revision < 0 or not isinstance(draft.state, str)
                or draft.state not in ('unknown', 'in_progress', 'completed', 'abandoned')
                or not isinstance(draft.progress, tuple) or not isinstance(draft.pending, tuple)
                or not isinstance(draft.dependencies, tuple) or len(draft.dependencies) > 1024
                or not isinstance(draft.consumed_checkpoints, tuple) or len(draft.consumed_checkpoints) > 16
                or not isinstance(draft.goal_role, str) or draft.goal_role not in ('explicit_task_goal', 'initial_user_request')
                or len(draft.progress) > 32 or len(draft.pending) > 32
                or draft.state == 'completed' and draft.pending):
            raise MemoryFailure('invalid_continuation')
        _time(draft.expires_at)
        for checkpoint_id in draft.consumed_checkpoints:
            _identifier(checkpoint_id)
        for dependency in draft.dependencies:
            if (not isinstance(dependency, tuple) or len(dependency) != 3
                    or any(type(version) is not int or version < 1 for version in dependency[1:])):
                raise MemoryFailure('invalid_continuation_source')
            _identifier(dependency[0])
        spans = (draft.goal, *draft.progress, *draft.pending)
        total = 0
        for span in spans:
            if (not isinstance(span, EvidenceSpan) or type(span.state_version) is not int or span.state_version < 1
                    or type(span.content_version) is not int or span.content_version < 1 or span.representation != 'original'
                    or type(span.start) is not int or type(span.end) is not int or not 0 <= span.start < span.end
                    or span.end - span.start > 16384 or not isinstance(span.expected_text, str)
                    or len(span.expected_text) != span.end - span.start):
                raise MemoryFailure('invalid_continuation_source')
            _identifier(span.message_id)
            try:
                span.expected_text.encode('utf-8')
            except UnicodeError:
                raise MemoryFailure('invalid_encoding') from None
            total += len(span.expected_text)
        if total > 131072:
            raise MemoryFailure('invalid_continuation')

    @staticmethod
    def _latest(db: sqlite3.Connection, task_id: str, branch_id: str) -> sqlite3.Row:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='continuation_checkpoints_v1'").fetchone():
            raise MemoryFailure('continuation_not_found')
        row = db.execute('SELECT * FROM continuation_checkpoints_v1 WHERE task_id=? AND branch_id=? '
            'ORDER BY revision DESC LIMIT 1', (task_id, branch_id)).fetchone()
        if row is None:
            raise MemoryFailure('continuation_not_found')
        assert isinstance(row, sqlite3.Row)
        return row

    @staticmethod
    def _source(db: sqlite3.Connection, span: dict[str, Any]) -> dict[str, Any]:
        state = MemoryRuntime._state(db, span['message_id'])
        row = db.execute('SELECT event,receipt FROM messages WHERE id=?', (span['message_id'],)).fetchone()
        message = MemoryRuntime._stored(row)
        if (state.status != 'active' or state.version != span['state_version']
                or not message.receipt.original_saved or message.receipt.content_version != span['content_version']
                or message.event.completion != 'complete'):
            raise MemoryFailure('stale_continuation_source')
        text = message.event.text[span['start']:span['end']]
        if len(text) != span['end'] - span['start']:
            raise MemoryFailure('invalid_continuation_source')
        return {**span, 'text': text, 'total_chars': len(message.event.text),
            'bounded_excerpt': span['start'] != 0 or span['end'] != len(message.event.text)}

    @staticmethod
    def _dependency(db: sqlite3.Connection, dependency: tuple[str, int, int] | list[Any]) -> None:
        message_id, state_version, content_version = dependency
        state = MemoryRuntime._state(db, message_id)
        row = db.execute('SELECT event,receipt FROM messages WHERE id=?', (message_id,)).fetchone()
        message = MemoryRuntime._stored(row)
        if state.status != 'active' or state.version != state_version or message.receipt.content_version != content_version:
            raise MemoryFailure('stale_continuation_source')

    def _view(self, db: sqlite3.Connection, row: sqlite3.Row) -> ContinuationView:
        record = json.loads(row['record'])
        state = ('abandoned' if record['state'] == 'abandoned' else
            'expired' if _time(record['expires_at']) <= time.time() else None)
        if state is not None:
            return ContinuationView(row['id'], row['task_id'], row['branch_id'], row['revision'], state,
                record['expires_at'], {}, (), (), row['request_id'], goal_role=record['goal_role'])
        omitted = []
        for span in (record['goal'], *record['progress'], *record['pending']):
            try:
                self._source(db, span)
            except MemoryFailure as error:
                if error.code not in {'stale_continuation_source', 'message_not_found'}:
                    raise
                omitted.append(str(span['message_id']))
        for dependency in record['dependencies']:
            try:
                self._dependency(db, dependency)
            except MemoryFailure as error:
                if error.code not in {'stale_continuation_source', 'message_not_found'}:
                    raise
                omitted.append(str(dependency[0]))
        if omitted:
            return ContinuationView(row['id'], row['task_id'], row['branch_id'], row['revision'], 'source_unavailable',
                record['expires_at'], {}, (), (), row['request_id'], goal_role=record['goal_role'],
                omitted_sources=tuple(dict.fromkeys(omitted)))
        return ContinuationView(row['id'], row['task_id'], row['branch_id'], row['revision'], record['state'],
            record['expires_at'], self._source(db, record['goal']),
            tuple(self._source(db, item) for item in record['progress']),
            tuple(self._source(db, item) for item in record['pending']), row['request_id'], goal_role=record['goal_role'])

    def save(self, access: Access, request_id: str, draft: ContinuationDraft) -> ContinuationView:
        identity = self.runtime._require_scope(access, 'capture', 'continuation_save')
        self.runtime._require_scope(access, 'recall', 'continuation_save')
        self._validate(draft)
        _identifier(request_id)
        for item in (draft.task_id, draft.branch_id, draft.anchor_id):
            _identifier(item)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(draft)).encode(), hashlib.sha256)
        checkpoint_id = 'continuation:' + hmac.digest(self.runtime._fingerprint_key,
            _json([identity.client_id, request_id]).encode(), hashlib.sha256).hex()
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            existing = db.execute('SELECT * FROM continuation_checkpoints_v1 WHERE owner=? AND request_id=?',
                (identity.client_id, request_id)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing['fingerprint']), fingerprint):
                    raise MemoryFailure('continuation_request_conflict')
                return self._view(db, existing)
            previous = db.execute('SELECT owner,revision FROM continuation_checkpoints_v1 WHERE task_id=? AND branch_id=? '
                'ORDER BY revision DESC LIMIT 1', (draft.task_id, draft.branch_id)).fetchone()
            if previous is not None and previous['owner'] != identity.client_id:
                raise MemoryFailure('continuation_branch_owner')
            revision = 0 if previous is None else int(previous['revision'])
            if revision != draft.expected_revision:
                raise MemoryFailure('continuation_revision_conflict')
            anchor = db.execute('SELECT event,receipt FROM messages WHERE id=?', (draft.anchor_id,)).fetchone()
            anchor_message = None if anchor is None else MemoryRuntime._stored(anchor)
            if (anchor_message is None or anchor_message.event.source.entry != identity.entry
                    or not anchor_message.receipt.original_saved or anchor_message.event.completion != 'complete'
                    or MemoryRuntime._state(db, draft.anchor_id).status != 'active'):
                raise MemoryFailure('continuation_anchor_required')
            goal = db.execute('SELECT event,receipt FROM messages WHERE id=?', (draft.goal.message_id,)).fetchone()
            if goal is None or MemoryRuntime._stored(goal).event.role != 'user':
                raise MemoryFailure('continuation_user_goal_required')
            record = asdict(draft)
            dependencies = list(draft.dependencies)
            for consumed_id in draft.consumed_checkpoints:
                consumed = db.execute('SELECT * FROM continuation_checkpoints_v1 WHERE id=?', (consumed_id,)).fetchone()
                if consumed is None:
                    raise MemoryFailure('continuation_not_found')
                if self._view(db, consumed).state not in ('unknown', 'in_progress', 'completed'):
                    raise MemoryFailure('stale_continuation_source')
                parent = json.loads(consumed['record'])
                dependencies.extend(tuple(item) for item in parent['dependencies'])
                for span in (parent['goal'], *parent['progress'], *parent['pending']):
                    dependencies.append((span['message_id'], span['state_version'], span['content_version']))
            dependencies.append((draft.anchor_id, MemoryRuntime._state(db, draft.anchor_id).version,
                anchor_message.receipt.content_version))
            record['dependencies'] = tuple(dict.fromkeys(dependencies))
            if len(record['dependencies']) > 1024:
                raise MemoryFailure('invalid_continuation_source')
            for dependency in draft.dependencies:
                self._dependency(db, dependency)
            for span in [record['goal'], *record['progress'], *record['pending']]:
                expected = span.pop('expected_text')
                if self._source(db, span)['text'] != expected:
                    raise MemoryFailure('continuation_span_mismatch')
            db.execute('INSERT INTO continuation_checkpoints_v1 VALUES (?,?,?,?,?,?,?,?)',
                (checkpoint_id, identity.client_id, request_id, fingerprint, draft.task_id, draft.branch_id,
                    revision + 1, _json(record)))
            result = self._view(db, self._latest(db, draft.task_id, draft.branch_id))
            db.execute('COMMIT')
            return result

    def current(self, access: Access, task_id: str, branch_id: str) -> ContinuationView:
        self.runtime._require_scope(access, 'recall', 'continuation_current')
        _identifier(task_id)
        _identifier(branch_id)
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            return self._view(db, self._latest(db, task_id, branch_id))
