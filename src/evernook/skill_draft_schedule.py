"""Review-only tool draft requests recorded with the captured original."""
from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3
import time

from .consolidation import ConsolidationWorkflow
from .experience import ExperienceDecision, ExperienceRegistry
from .models import Access, CaptureReceipt, Event, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json
from .skill_drafts import SkillDraftProposer


_POLICY = 'skill_draft_policy_v1'
_QUEUE = 'skill_draft_queue_v1'
_INVALIDATED = {'stale_experience_source', 'message_not_found', 'experience_span_mismatch'}


def _policy(db: sqlite3.Connection) -> tuple[bool, tuple[str, ...]]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (_POLICY,)).fetchone():
        return False, ()
    row = db.execute(f'SELECT enabled,tool_names FROM {_POLICY} WHERE id=1').fetchone()
    if row is None:
        return False, ()
    try:
        tools = json.loads(row[1])
    except (TypeError, ValueError):
        raise MemoryFailure('invalid_skill_draft_policy') from None
    if row[0] not in (0, 1) or not isinstance(tools, list) or any(not isinstance(name, str) for name in tools):
        raise MemoryFailure('invalid_skill_draft_policy')
    return bool(row[0]), tuple(tools)


def _queue_exists(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (_QUEUE,)).fetchone() is not None


def enqueue(db: sqlite3.Connection, event: Event, receipt: CaptureReceipt) -> None:
    """Use the capture's transaction so a saved result cannot outrun its request."""
    if receipt.gaps != ('incomplete_output',):
        return
    try:
        enabled, allowed = _policy(db)
    except MemoryFailure as failure:
        if failure.code != 'invalid_skill_draft_policy':
            raise
        return  # The optional policy is diagnosed by status; preserve the original capture.
    if enabled and receipt.tool_name in allowed and _queue_exists(db):
        db.execute(f'INSERT INTO {_QUEUE} VALUES (?,?,?,?,?)', (receipt.message.message_id,
            receipt.tool_name, event.source.entry, event.source.session, receipt.message.received_at))


def discard(db: sqlite3.Connection, result_id: str) -> None:
    if _queue_exists(db):
        db.execute(f'DELETE FROM {_QUEUE} WHERE result_id=?', (result_id,))


def strip_export(db: sqlite3.Connection) -> None:
    """Export evidence and review outcomes without live scheduling settings or work."""
    db.execute(f'DROP TABLE IF EXISTS {_QUEUE}')
    db.execute(f'DROP TABLE IF EXISTS {_POLICY}')


class SkillDraftSchedule:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    def configure(self, access: Access, *, enabled: bool = True,
                  tool_names: tuple[str, ...] | None = None) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'skill_draft_configure')
        self.runtime._require_scope(access, 'accept', 'skill_draft_configure')
        if type(enabled) is not bool or (tool_names is not None and (
                not isinstance(tool_names, tuple) or len(tool_names) > 16
                or any(not isinstance(name, str) for name in tool_names))):
            raise MemoryFailure('invalid_skill_draft_policy')
        if tool_names is not None:
            for name in tool_names:
                _identifier(name)
            if 'evernook_memory_reflect' in tool_names or len(set(tool_names)) != len(tool_names):
                raise MemoryFailure('invalid_skill_draft_policy')
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not {'maintain', 'accept'}.issubset(self.runtime._authorize(access).scopes):
                raise MemoryFailure('permission_denied')
            names = _policy(db)[1] if tool_names is None else tuple(sorted(tool_names))
            if enabled and not names:
                raise MemoryFailure('invalid_skill_draft_policy')
            db.execute(f'''CREATE TABLE IF NOT EXISTS {_POLICY} (
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, tool_names TEXT NOT NULL)''')
            db.execute(f'''CREATE TABLE IF NOT EXISTS {_QUEUE} (
                result_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                tool_name TEXT NOT NULL, entry TEXT NOT NULL, session TEXT NOT NULL, received TEXT NOT NULL)''')
            db.execute(f'''INSERT INTO {_POLICY} VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,tool_names=excluded.tool_names''', (enabled, _json(names)))
            db.execute('COMMIT')
        return self.status(access)

    def status(self, access: Access) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'skill_draft_status')
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            enabled, tools = _policy(db)
            has_queue = _queue_exists(db)
            if enabled and not has_queue:
                raise MemoryFailure('skill_draft_schedule_unavailable')
            pending = int(db.execute(f'SELECT count(*) FROM {_QUEUE}').fetchone()[0]) if has_queue else 0
            return {'enabled': enabled, 'tool_names': tools, 'pending': pending, 'publication': 'review_only'}

    def _tick(self, access: Access, *, reason: str, scheduler_token: str, deadline: float,
              available: Callable[[sqlite3.Connection], bool]) -> None:
        required = {'maintain', 'recall', 'accept'}
        def check() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryFailure('scheduler_time_budget')
            return remaining

        proposer, registry = SkillDraftProposer(self.runtime), ExperienceRegistry(self.runtime)
        prepared: list[tuple[str, ExperienceDecision | None]] = []
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            enabled, tools = _policy(db)
            if not enabled or not required.issubset(self.runtime._authorize(access).scopes) or not available(db):
                return
            if not _queue_exists(db):
                raise MemoryFailure('skill_draft_schedule_unavailable')
            boundary = ('''AND EXISTS (SELECT 1 FROM scheduler_sessions_v1 s WHERE s.entry=q.entry
                           AND s.session=q.session AND s.marked_at>=q.received)''' if reason == 'session_end' else '')
            for row in db.execute(f'''SELECT q.result_id FROM {_QUEUE} q
                WHERE q.tool_name IN (SELECT value FROM json_each(?)) {boundary}
                ORDER BY q.rowid LIMIT 4''', (_json(tools),)):
                check()
                result_id = str(row[0])
                prepared.append((result_id, proposer._proposal(db, result_id)))
        if not prepared:
            return
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN IMMEDIATE')
            ConsolidationWorkflow._scheduler_fence(db, scheduler_token)
            if (_policy(db) != (True, tools) or not available(db)
                    or not required.issubset(self.runtime._authorize(access).scopes)):
                return
            for result_id, decision in prepared:
                check()
                if not db.execute(f'SELECT 1 FROM {_QUEUE} WHERE result_id=?', (result_id,)).fetchone():
                    continue
                if decision is not None:
                    registry._validate_decision(decision)
                    try:
                        registry._write_decision(db, decision)
                    except MemoryFailure as failure:
                        if failure.code not in _INVALIDATED:
                            raise
                db.execute(f'DELETE FROM {_QUEUE} WHERE result_id=?', (result_id,))
            check()
            db.execute('COMMIT')
