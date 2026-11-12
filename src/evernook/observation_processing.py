"""Optional bounded finite-rule observation candidates; no model inference admission or publication."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .automatic_preferences import PreferenceUpdates
from .candidates import CandidateDraft, CandidateWorkflow, EvidenceSpan
from .models import Access, MemoryFailure
from .observations import ObservationDraft, ObservationRecord, ObservationRegistry, source_contexts
from .preference_statements import assess_preferences
from .runtime import MemoryRuntime, _identifier, _json


class ObservationProcessing:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.registry = ObservationRegistry(runtime)
        self.workflow = CandidateWorkflow(runtime)

    @property
    def processor(self) -> str:
        digest = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('observations.py').read_bytes()
            + PreferenceUpdates(self.runtime).processor.processor_version.encode()).hexdigest()
        return 'observation-rules-v1:' + digest

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        ObservationRegistry._schema(db)
        db.execute('CREATE TABLE IF NOT EXISTS observation_policy_v1 (id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS observation_progress_v1 (message_id TEXT NOT NULL,processor TEXT NOT NULL,
            state_version INTEGER NOT NULL,content_version INTEGER NOT NULL,run_id TEXT NOT NULL,
            PRIMARY KEY(message_id,processor))''')
        db.execute('''CREATE TABLE IF NOT EXISTS observation_atoms_v1 (message_id TEXT NOT NULL,processor TEXT NOT NULL,
            start INTEGER NOT NULL,end INTEGER NOT NULL,topic_key TEXT NOT NULL,category TEXT NOT NULL,
            stance TEXT NOT NULL,span TEXT NOT NULL,PRIMARY KEY(message_id,processor,start,end))''')
        db.execute('CREATE INDEX IF NOT EXISTS observation_atom_topics_v1 ON observation_atoms_v1(processor,topic_key)')
        db.execute('CREATE TABLE IF NOT EXISTS observation_runs_v1 (run_id TEXT PRIMARY KEY,observation_ids TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS observation_blocked_v1 (topic_key TEXT PRIMARY KEY,processor TEXT NOT NULL,run_id TEXT NOT NULL,reason TEXT NOT NULL)')

    def configure(self, access: Access, *, enabled: bool) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'observation_configure')
        self.runtime._require_scope(access, 'accept', 'observation_configure')
        if type(enabled) is not bool:
            raise MemoryFailure('invalid_observation_policy')
        with self.runtime._connection() as db:
            self._schema(db)
            db.execute('INSERT INTO observation_policy_v1 VALUES (1,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled', (int(enabled),))
        return self.status(access)

    def status(self, access: Access) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'observation_status')
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_policy_v1'").fetchone()
            row = db.execute('SELECT enabled FROM observation_policy_v1 WHERE id=1').fetchone() if exists else None
            states = ({str(item[0]): int(item[1]) for item in db.execute('''SELECT e.state,count(*) FROM observation_events_v1 e
                WHERE NOT EXISTS (SELECT 1 FROM observation_events_v1 newer WHERE newer.observation_id=e.observation_id
                    AND newer.revision>e.revision) GROUP BY e.state''')} if exists else {})
            return {'enabled': bool(row and row[0]), 'processor': self.processor, 'publication': 'review_only',
                'completed_runs': db.execute('SELECT count(*) FROM observation_runs_v1').fetchone()[0] if exists else 0,
                'stored_heads_by_state': states,
                'blocked_topics': db.execute('SELECT count(*) FROM observation_blocked_v1').fetchone()[0] if exists else 0,
                'source_atoms': db.execute('SELECT count(*) FROM observation_atoms_v1').fetchone()[0] if exists else 0}

    def tick(self, access: Access, *, limit: int = 4, timeout: float = 1,
             cancel: threading.Event | None = None) -> dict[str, Any]:
        identity = self.runtime._require_scope(access, 'maintain', 'observation_process')
        self.runtime._require_scope(access, 'recall', 'observation_process')
        if type(limit) is not int or not 1 <= limit <= 16 or type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise MemoryFailure('invalid_observation_budget')
        deadline = time.monotonic() + timeout
        def budget() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('observation_canceled')
            left = deadline - time.monotonic()
            if left <= 0:
                raise MemoryFailure('observation_timeout')
            return left
        budget()
        processor = self.processor
        with self.runtime._connection(check=budget) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_policy_v1'").fetchone():
                return {'state': 'disabled', 'processed': 0, 'published': 0}
            policy = db.execute('SELECT enabled FROM observation_policy_v1 WHERE id=1').fetchone()
            if not policy or not policy[0]:
                return {'state': 'disabled', 'processed': 0, 'published': 0}
            ids = tuple(str(row[0]) for row in db.execute('''SELECT m.id FROM messages m LEFT JOIN visibility v ON v.message_id=m.id
                LEFT JOIN observation_progress_v1 p ON p.message_id=m.id AND p.processor=?
                AND p.state_version=COALESCE(v.version,1) AND p.content_version=json_extract(m.receipt,'$.content_version')
                WHERE p.message_id IS NULL AND COALESCE(v.status,'active')='active'
                AND json_extract(m.event,'$.role')='user' AND json_extract(m.event,'$.completion')='complete'
                AND json_extract(m.receipt,'$.original_saved')=1 AND json_extract(m.receipt,'$.representation')='original'
                ORDER BY m.received,m.id LIMIT ?''', (processor, limit)))
            selected, count = [], 0
            for source in self.workflow._sources(db, ids):
                contribution = sum(item.scope in {'long_term', 'withdrawal'} and not item.normalized.startswith('我希望')
                    for item in assess_preferences(source.message.event.text).statements)
                if count + contribution > 32:
                    break
                selected.append(source.message.receipt.message_id)
                count += contribution
            ids = tuple(selected)
        if not ids:
            return {'state': 'idle', 'processed': 0, 'published': 0}
        lease = self.workflow.begin(access, ids, processor_version=processor, lease_seconds=min(3600, int(timeout) + 30))
        if lease.token is None:
            # This is normally covered by progress; a missing receipt is a corruption, never a new vote.
            with self.runtime._connection() as db:
                row = db.execute('SELECT observation_ids FROM observation_runs_v1 WHERE run_id=?', (lease.status.run_id,)).fetchone()
                if row is None:
                    raise MemoryFailure('observation_run_incomplete')
                return {'state': 'complete', 'processed': 0, 'published': 0, 'run_id': lease.status.run_id,
                    'observation_ids': json.loads(row[0]), 'replayed': True}
        candidates: list[CandidateDraft] = []
        atoms = []
        for source in lease.sources:
            event, receipt = source.message.event, source.message.receipt
            for statement in assess_preferences(event.text).statements:
                if statement.scope not in {'long_term', 'withdrawal'} or statement.normalized.startswith('我希望'):
                    continue
                span = EvidenceSpan(receipt.message_id, source.state_version, 'original', receipt.content_version,
                    statement.start, statement.end, statement.text)
                key, _ = PreferenceUpdates._key_value(statement.text, statement.category)
                candidates.append(CandidateDraft(statement.text, 'user_statement', (span,)))
                atoms.append((receipt.message_id, processor, span.start, span.end, key, statement.category,
                    'counterevidence' if statement.negative else 'support', _json(asdict(span))))
        try:
            budget()
            with self.runtime._connection(check=budget) as db:
                db.execute('BEGIN IMMEDIATE')
                self._schema(db)
                self.workflow._complete(db, identity.client_id, lease.status.run_id, lease.token, tuple(candidates))
                if not db.execute('SELECT enabled FROM observation_policy_v1 WHERE id=1').fetchone()[0]:
                    raise MemoryFailure('observation_paused')
                db.executemany('DELETE FROM observation_atoms_v1 WHERE message_id=? AND processor=?', ((message_id, processor) for message_id in ids))
                db.executemany('INSERT INTO observation_atoms_v1 VALUES (?,?,?,?,?,?,?,?)', atoms)
                observations, blocked_topics = [], []
                for key in dict.fromkeys(str(atom[4]) for atom in atoms):
                    if db.execute('SELECT 1 FROM observation_blocked_v1 WHERE topic_key=?', (key,)).fetchone():
                        blocked_topics.append(key)
                        continue
                    selected_id = self.registry._id(key, ())
                    request_id = 'observation-run:' + lease.status.run_id + ':' + hashlib.sha256(key.encode()).hexdigest()[:16]
                    if db.execute('SELECT 1 FROM observation_requests_v1 WHERE request_id=?', (request_id,)).fetchone():
                        observations.append(selected_id)
                        continue
                    support: list[EvidenceSpan] = []
                    counters: list[EvidenceSpan] = []
                    category = ''
                    overflow = False
                    for atom in db.execute('SELECT * FROM observation_atoms_v1 WHERE processor=? AND topic_key=? ORDER BY message_id,start', (processor, key)):
                        budget()
                        span = EvidenceSpan(**json.loads(atom['span']))
                        try:
                            source_contexts(db, (span,), atom['stance'])
                        except MemoryFailure as failure:
                            if failure.code in {'stale_observation_source', 'message_not_found', 'observation_span_mismatch'}:
                                continue
                            raise
                        (support if atom['stance'] == 'support' else counters).append(span)
                        category = str(atom['category'])
                        if len(support) + len(counters) > 64:
                            overflow = True
                            break
                    previous = self.registry._latest(db, selected_id)
                    if previous and previous.state == 'withdrawn':
                        continue  # A worker cannot resurrect an explicit user withdrawal or split.
                    if previous:
                        for stance, spans in (('support', previous.draft.support), ('counterevidence', previous.draft.counterevidence)):
                            for span in spans:
                                try:
                                    source_contexts(db, (span,), stance)
                                except MemoryFailure:
                                    continue
                                target = support if stance == 'support' else counters
                                if span not in target:
                                    target.append(span)
                    if len(support) + len(counters) > 64:
                        overflow = True
                    if overflow:
                        db.execute('INSERT INTO observation_blocked_v1 VALUES (?,?,?,?)', (key, processor, lease.status.run_id, 'evidence_limit'))
                        # Full atoms (including the overflowing denial) remain available through evidence().
                        # Withdraw the usable head instead of publishing a truncated belief or blocking other slots.
                        placeholder = ObservationDraft(key, '证据超限，需拆分后审查。', tuple(support[:1]), tuple(counters[:1]), category,
                            None, None, processor, 'observation-review-only-v1')
                        record = ObservationRecord(selected_id, previous.revision + 1 if previous else 1, 'needs_review',
                            'evidence_limit', previous.draft if previous else placeholder, datetime.now(timezone.utc).isoformat())
                        self.registry._append(db, record, request_id, hashlib.sha256(_json(asdict(record)).encode()).hexdigest())
                        blocked_topics.append(key)
                        observations.append(selected_id)
                        continue
                    excerpts = list(dict.fromkeys(span.expected_text for span in (*support, *counters)))
                    conclusion = '偏好陈述综合（含反例则保持冲突，不推断频率）：' + '；'.join(excerpts)
                    values = [PreferenceUpdates._key_value(span.expected_text, category)[1] for span in (*support, *counters)]
                    disputed = any(PreferenceUpdates._conflicts(left, right, category) for index, left in enumerate(values)
                        for right in values[index + 1:])
                    draft = self.registry._canonical(ObservationDraft(key, conclusion, tuple(support), tuple(counters), category,
                        None, None, processor, 'observation-review-only-v1', disputed=disputed))
                    operation = 'create' if previous is None else 'merge'
                    if previous and self.registry._view(db, previous).state == 'unavailable':
                        operation = 'correction'
                    record = ObservationRecord(selected_id, previous.revision + 1 if previous else 1, 'candidate', operation,
                        draft, datetime.now(timezone.utc).isoformat())
                    self.registry._append(db, record, request_id, hashlib.sha256(_json(asdict(record)).encode()).hexdigest())
                    observations.append(selected_id)
                db.executemany('INSERT INTO observation_progress_v1 VALUES (?,?,?,?,?) ON CONFLICT(message_id,processor) DO UPDATE SET '
                    'state_version=excluded.state_version,content_version=excluded.content_version,run_id=excluded.run_id',
                    ((source.message.receipt.message_id, processor, source.state_version, source.message.receipt.content_version,
                      lease.status.run_id) for source in lease.sources))
                db.execute('INSERT INTO observation_runs_v1 VALUES (?,?)', (lease.status.run_id, _json(observations)))
                self.workflow._lease(self.workflow._row(db, lease.status.run_id), identity.client_id, lease.token)
                budget()
                db.execute('COMMIT')
                return {'state': 'complete', 'processed': len(ids), 'extracted': len(candidates), 'candidates': len(observations),
                    'published': 0, 'run_id': lease.status.run_id, 'observation_ids': observations, 'blocked_topics': blocked_topics}
        except Exception:
            try:
                self.workflow.fail(access, lease.status.run_id, lease.token, error_code='extractor_failed')
            except MemoryFailure:
                pass
            raise

    def evidence(self, access: Access, topic_key: str, *, limit: int = 32, offset: int = 0,
                 check: Callable[[], float] | None = None) -> dict[str, Any]:
        self.runtime._require_scope(access, 'maintain', 'observation_evidence')
        self.runtime._require_scope(access, 'recall', 'observation_evidence')
        _identifier(topic_key)
        if type(limit) is not int or not 1 <= limit <= 32 or type(offset) is not int or offset < 0:
            raise MemoryFailure('invalid_page')
        with self.runtime._connection(check=check) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_atoms_v1'").fetchone():
                return {'rows': [], 'next_offset': None}
            atoms = db.execute('''SELECT a.span,a.stance FROM observation_atoms_v1 a JOIN messages m ON m.id=a.message_id
                WHERE a.topic_key=? GROUP BY a.span,a.stance ORDER BY m.received,m.id,a.start LIMIT ? OFFSET ?''',
                (topic_key, limit + 1, offset)).fetchall()
            rows: list[dict[str, Any]] = []
            for atom in atoms[:limit]:
                try:
                    rows.extend(source_contexts(db, (EvidenceSpan(**json.loads(atom['span'])),), atom['stance']))
                except MemoryFailure as failure:
                    if failure.code not in {'stale_observation_source', 'message_not_found', 'observation_span_mismatch'}:
                        raise
            return {'rows': rows, 'next_offset': offset + limit if len(atoms) > limit else None,
                'coverage': 'paged_active_raw_atoms_including_overflow'}
