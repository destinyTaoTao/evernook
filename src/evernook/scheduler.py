"""In-process consolidation scheduling: session-end and idle triggers, pause/resume, crash-safe resume."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
import secrets
import sqlite3
import time

from .candidates import CandidateLease
from .consolidation import CONSOLIDATION_KINDS, ConsolidationDraft, ConsolidationWorkflow
from .automatic_preferences import PreferenceUpdate, PreferenceUpdates
from .models import Access, LexicalStatus, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _RECALLABLE, _time


SCHEDULER_REASONS = ("session_end", "turn_end", "startup", "idle", "manual")
_SCHEDULER_DEFAULT_SOURCES = 8
_SCHEDULER_DEFAULT_RUNS = 1
_PENDING_SOURCES = f"jobs.state='pending' AND messages.id NOT IN (SELECT message_id FROM scheduler_progress_v1) AND {_RECALLABLE}"


@dataclass(frozen=True)
class _Settings:
    paused: bool = False
    max_sources_per_run: int = _SCHEDULER_DEFAULT_SOURCES
    max_runs_per_trigger: int = _SCHEDULER_DEFAULT_RUNS
    min_pending: int = 1
    idle_seconds: int = 0
    max_trigger_seconds: float = 10


@dataclass(frozen=True)
class SchedulerStatus:
    paused: bool
    max_sources_per_run: int
    max_runs_per_trigger: int
    processed: int
    session_marks: int
    skip_reasons: dict[str, int] = field(default_factory=dict)
    pending: int = 0
    oldest_pending_seconds: float | None = None
    lexical: LexicalStatus | None = None
    min_pending: int = 1
    idle_seconds: int = 0
    max_trigger_seconds: float = 10
    foreground_sessions: int = 0
    covered_messages: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    preference_updates: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerTick:
    reason: str
    skipped: str | None
    run_ids: tuple[str, ...]
    processed: int
    remaining: int
    preference_updates: tuple[PreferenceUpdate, ...] = ()


def extractive_provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
    """Deterministic low-confidence topic placeholders; real generation stays with the trusted operator or model path."""
    drafts: list[ConsolidationDraft] = []
    for source in lease.sources:
        text = source.message.event.text
        if source.message.event.role != "user" or not source.message.receipt.original_saved or not text.strip():
            continue
        end = min(1024, len(text))
        span = source.message.event
        from .candidates import EvidenceSpan
        drafts.append(ConsolidationDraft("topic", "主题：" + text[:24],
            (EvidenceSpan(source.message.receipt.message_id, source.state_version, "original",
                          source.message.receipt.content_version, 0, end, text[:end]),),
            confidence=0.3))
    return tuple(drafts)


class ConsolidationScheduler:
    """No OS scheduler, no daemon: hosts and the trusted local operator call trigger explicitly."""

    def __init__(self, runtime: MemoryRuntime, workflow: ConsolidationWorkflow | None = None):
        self.runtime = runtime
        self.workflow = workflow or ConsolidationWorkflow(runtime)

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_settings_v1 (
            id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL,
            max_sources_per_run INTEGER NOT NULL, max_runs_per_trigger INTEGER NOT NULL)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(scheduler_settings_v1)")}
        for name, definition in (("min_pending", "INTEGER NOT NULL DEFAULT 1"),
                                 ("idle_seconds", "INTEGER NOT NULL DEFAULT 0"),
                                 ("max_trigger_seconds", "REAL NOT NULL DEFAULT 10")):
            if name not in columns:
                db.execute(f"ALTER TABLE scheduler_settings_v1 ADD COLUMN {name} {definition}")
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_progress_v1 (
            message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL, processed_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_sessions_v1 (
            entry TEXT NOT NULL, session TEXT NOT NULL, marked_at TEXT NOT NULL,
            PRIMARY KEY(entry,session,marked_at))""")
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_foreground_v1 (
            owner TEXT NOT NULL,session TEXT NOT NULL,active_until REAL NOT NULL,last_activity REAL NOT NULL,
            PRIMARY KEY(owner,session))""")
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_lease_v1 (
            id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL,expires REAL NOT NULL)""")

    def _settings(self, db: sqlite3.Connection) -> _Settings:
        row = db.execute("SELECT * FROM scheduler_settings_v1 WHERE id=1").fetchone()
        if row is None:
            return _Settings()
        return _Settings(bool(row["paused"]), int(row["max_sources_per_run"]), int(row["max_runs_per_trigger"]),
                         int(row["min_pending"]), int(row["idle_seconds"]), float(row["max_trigger_seconds"]))

    def configure(self, access: Access, *, paused: bool | None = None, max_sources_per_run: int | None = None,
                  max_runs_per_trigger: int | None = None, min_pending: int | None = None,
                  idle_seconds: int | None = None, max_trigger_seconds: float | None = None) -> SchedulerStatus:
        self.runtime._require_scope(access, "maintain", "scheduler_configure")
        for value in (max_sources_per_run, max_runs_per_trigger):
            if value is not None and (type(value) is not int or not 1 <= value <= 16):
                raise MemoryFailure("invalid_scheduler_config")
        if paused is not None and type(paused) is not bool:
            raise MemoryFailure("invalid_scheduler_config")
        if (min_pending is not None and (type(min_pending) is not int or not 1 <= min_pending <= 1000000)
                or idle_seconds is not None and (type(idle_seconds) is not int or not 0 <= idle_seconds <= 86400)
                or max_trigger_seconds is not None and (type(max_trigger_seconds) not in (int, float)
                    or not math.isfinite(max_trigger_seconds) or not 0.1 <= max_trigger_seconds <= 30)):
            raise MemoryFailure("invalid_scheduler_config")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            current = self._settings(db)
            updated = _Settings(current.paused if paused is None else paused,
                current.max_sources_per_run if max_sources_per_run is None else max_sources_per_run,
                current.max_runs_per_trigger if max_runs_per_trigger is None else max_runs_per_trigger,
                current.min_pending if min_pending is None else min_pending,
                current.idle_seconds if idle_seconds is None else idle_seconds,
                current.max_trigger_seconds if max_trigger_seconds is None else float(max_trigger_seconds))
            db.execute("""INSERT INTO scheduler_settings_v1 VALUES (1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                paused=excluded.paused, max_sources_per_run=excluded.max_sources_per_run,
                max_runs_per_trigger=excluded.max_runs_per_trigger, min_pending=excluded.min_pending,
                idle_seconds=excluded.idle_seconds,max_trigger_seconds=excluded.max_trigger_seconds""",
                (int(updated.paused), updated.max_sources_per_run, updated.max_runs_per_trigger,
                 updated.min_pending, updated.idle_seconds, updated.max_trigger_seconds))
            db.execute("COMMIT")
        return self.status(access)

    def status(self, access: Access) -> SchedulerStatus:
        self.runtime._require_scope(access, "maintain", "scheduler_status")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self._schema(db)
            settings = self._settings(db)
            processed = int(db.execute("SELECT count(*) FROM scheduler_progress_v1").fetchone()[0])
            marks = int(db.execute("SELECT count(*) FROM scheduler_sessions_v1").fetchone()[0])
            reasons = {str(row["state"])[8:]: int(row["count"]) for row in db.execute(
                "SELECT state,count(*) AS count FROM scheduler_progress_v1 WHERE state LIKE 'skipped:%' GROUP BY state")}
            backlog = db.execute(f"""SELECT count(*),min(messages.received) FROM messages JOIN jobs ON jobs.message_id=messages.id
                WHERE {_PENDING_SOURCES}""").fetchone()
            oldest = None if backlog[1] is None else max(0.0, datetime.now(timezone.utc).timestamp() - _time(backlog[1]))
            covered = int(db.execute(f"""SELECT count(*) FROM messages WHERE {_RECALLABLE}
                AND id IN (SELECT message_id FROM scheduler_progress_v1)""").fetchone()[0])
            failures: dict[str, int] = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_runs_v1'").fetchone():
                failures = {str(row[0]): int(row[1]) for row in db.execute("""SELECT CASE WHEN state='running'
                    THEN 'lease_expired' ELSE error_code END AS reason,count(*) FROM candidate_runs_v1
                    WHERE processor='consolidation-scheduler-v1' AND (state='failed' OR (state='running' AND lease_until<=?))
                    GROUP BY reason""", (time.time(),))}
            return SchedulerStatus(settings.paused, settings.max_sources_per_run, settings.max_runs_per_trigger,
                processed, marks, reasons, int(backlog[0]), oldest, self.runtime._lexical_status(db),
                settings.min_pending, settings.idle_seconds, settings.max_trigger_seconds,
                foreground_sessions=self._foreground_count(db), covered_messages=covered, failure_reasons=failures,
                preference_updates=PreferenceUpdates(self.runtime)._status(db))

    @staticmethod
    def _foreground_count(db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT count(*) FROM scheduler_foreground_v1 WHERE active_until>?", (time.time(),)).fetchone()[0])

    def foreground(self, access: Access, *, session: str, active: bool, ttl_seconds: int = 30) -> dict[str, object]:
        """Capture-only host activity, owned by that caller; grants no processing authority."""
        identity = self.runtime._require_scope(access, "capture", "scheduler_foreground")
        _identifier(session)
        if type(active) is not bool or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
            raise MemoryFailure("invalid_scheduler_activity")
        now = time.time()
        until = now + ttl_seconds if active else 0.0
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            db.execute("DELETE FROM scheduler_foreground_v1 WHERE max(active_until,last_activity)<?", (now - 86400,))
            db.execute("""INSERT INTO scheduler_foreground_v1 VALUES (?,?,?,?) ON CONFLICT(owner,session) DO UPDATE SET
                active_until=excluded.active_until,last_activity=excluded.last_activity""", (identity.client_id, session, until, now))
            db.execute("COMMIT")
        return {"session": session, "active": active, "active_until": until}

    def pause(self, access: Access, *, paused: bool) -> SchedulerStatus:
        return self.configure(access, paused=paused)

    def session_end(self, access: Access, *, entry: str, session: str) -> dict[str, object]:
        """Capture-scope hook: hosts mark a session boundary; nothing is consolidated here."""
        identity = self.runtime._require_scope(access, "capture", "scheduler_session_end")
        _identifier(entry)
        _identifier(session)
        if entry != identity.entry:
            # Same boundary as submit: a caller marks only its own entry's sessions.
            raise MemoryFailure("unauthorized_source")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            db.execute("INSERT OR IGNORE INTO scheduler_sessions_v1 VALUES (?,?,?)",
                (entry, session, datetime.now(timezone.utc).isoformat()))
            db.execute("COMMIT")
        return {"entry": entry, "session": session, "marked": True}

    def trigger(self, access: Access, *, reason: str,
                provider: Callable[[CandidateLease], tuple[ConsolidationDraft, ...]] | None = None) -> SchedulerTick:
        """Serialize windows across hosts; expiry permits recovery after a killed worker."""
        self.runtime._require_scope(access, "maintain", "scheduler_trigger")
        token = secrets.token_urlsafe(32)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            previous = db.execute("SELECT expires FROM scheduler_lease_v1 WHERE id=1").fetchone()
            if previous is not None and float(previous[0]) > time.time():
                raise MemoryFailure("candidate_run_busy")
            ttl = math.ceil(self._settings(db).max_trigger_seconds) + 30
            db.execute("INSERT INTO scheduler_lease_v1 VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,expires=excluded.expires",
                       (token, time.time() + ttl))
            db.execute("COMMIT")
        try:
            updates: list[PreferenceUpdate] = []
            result = self._trigger(access, reason=reason, provider=provider, scheduler_token=token, preference_updates=updates)
            return replace(result, preference_updates=tuple(updates))
        finally:
            with self.runtime._connection() as db:
                db.execute("DELETE FROM scheduler_lease_v1 WHERE id=1 AND token=?", (token,))

    def _trigger(self, access: Access, *, reason: str, scheduler_token: str, preference_updates: list[PreferenceUpdate],
                 provider: Callable[[CandidateLease], tuple[ConsolidationDraft, ...]] | None = None) -> SchedulerTick:
        """Bounded consolidation pass over unprocessed pending raw jobs; idempotent and crash-resumable.

        Trigger reasons are labels: the host owns trigger timing and session marks are observability
        bookkeeping, not a queue this method drains. Ineligible sources are individually skipped
        before batching; transient provider failures leave eligible evidence retryable.
        """
        started = time.monotonic()
        self.runtime._require_scope(access, "maintain", "scheduler_trigger")
        if not isinstance(reason, str) or reason not in SCHEDULER_REASONS:
            raise MemoryFailure("invalid_scheduler_reason")
        if provider is not None and not callable(provider):
            raise MemoryFailure("invalid_scheduler_config")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self._schema(db)
            settings = self._settings(db)
            deadline = started + settings.max_trigger_seconds
            sources_limit, runs_limit = settings.max_sources_per_run, settings.max_runs_per_trigger
            if settings.paused:
                db.execute("COMMIT")
                return SchedulerTick(reason, "paused", (), 0, self._remaining(db))
            if self._foreground_count(db):
                db.execute("COMMIT")
                return SchedulerTick(reason, "foreground", (), 0, self._remaining(db))
            # Old versions skipped whole mixed batches without a reason. Recheck those sources;
            # new per-source reasons and verified empty results make this a one-time recovery.
            db.execute("DELETE FROM scheduler_progress_v1 WHERE state='skipped'")
            preferences = PreferenceUpdates(self.runtime)
            promotions = preferences._pending(db, sources_limit * runs_limit)
            raw_ready = reason == 'manual' or self._remaining(db) >= settings.min_pending
            if not raw_ready and not promotions:
                db.execute("COMMIT")
                return SchedulerTick(reason, "threshold", (), 0, self._remaining(db))
            if reason == "idle" and settings.idle_seconds:
                latest = db.execute("SELECT max(received) FROM messages").fetchone()[0]
                foreground = db.execute("SELECT max(max(active_until,last_activity)) FROM scheduler_foreground_v1").fetchone()[0]
                last_activity = max(_time(latest) if latest is not None else 0, float(foreground or 0))
                if time.time() - last_activity < settings.idle_seconds:
                    db.execute("COMMIT")
                    return SchedulerTick(reason, "idle_wait", (), 0, self._remaining(db))
            candidates = [] if not raw_ready else db.execute(f"""SELECT messages.id,messages.event,messages.receipt FROM messages JOIN jobs ON jobs.message_id=messages.id
                WHERE {_PENDING_SOURCES} ORDER BY messages.received, messages.id LIMIT ?""",
                (sources_limit * runs_limit,)).fetchall()
            completed_runs: list[str] = []
            if candidates and db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_runs_v1'").fetchone():
                placeholders = ','.join('?' for _ in candidates)
                completed_runs = [str(row[0]) for row in db.execute(f"""SELECT DISTINCT r.id FROM candidate_runs_v1 r,
                    json_each(r.sources) s WHERE r.processor='consolidation-scheduler-v1' AND r.state='succeeded'
                    AND json_extract(s.value,'$[0]') IN ({placeholders}) ORDER BY r.id""",
                    tuple(row['id'] for row in candidates))]
            db.execute("COMMIT")
        source_ids: list[str] = []
        preference_ids: list[str] = []
        progress: list[tuple[str, str, str]] = []
        for row in candidates:
            message = self.runtime._stored(row)
            skip_reason = None
            if message.event.role not in {"user", "assistant", "tool"}:
                skip_reason = "unsupported_role"
            elif message.event.completion != "complete":
                skip_reason = "incomplete_source"
            elif not message.receipt.original_saved:
                skip_reason = "protected_projection"
            elif not message.event.text.strip():
                skip_reason = "empty_source"
            if skip_reason is None:
                source_ids.append(str(row["id"]))
                if message.event.role == 'user' and PreferenceUpdates.classify_statement(message.event.text) is not None:
                    preference_ids.append(str(row['id']))
            else:
                progress.append((str(row["id"]), "", "skipped:" + skip_reason))
        run_ids: list[str] = []
        def guarded_provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                raise MemoryFailure("scheduler_" + interrupted)
            drafts = (provider or extractive_provider)(lease)
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                raise MemoryFailure("scheduler_" + interrupted)
            return drafts
        for message_id in preference_ids:
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                return SchedulerTick(reason, interrupted, (), 0, self._remaining_count([]))
            try:
                report = preferences.process(access, (message_id,), timeout=min(120, max(0.001, deadline - time.monotonic())),
                    scheduler_token=scheduler_token)
            except MemoryFailure as error:
                if error.code in {'scheduler_foreground', 'scheduler_paused', 'scheduler_time_budget'}:
                    return SchedulerTick(reason, error.code.removeprefix('scheduler_'), (), 0, self._remaining_count([]))
                raise
            if report.error_code == 'extraction_timeout':
                return SchedulerTick(reason, 'time_budget', (), 0, self._remaining_count([]))
            if report.error_code is not None or not report.runs or any(run.state != 'succeeded' for run in report.runs):
                raise MemoryFailure('preference_processing_failed')
            preference_updates.extend(report.updates)
        for candidate_id in promotions[:max(0, sources_limit * runs_limit - len(candidates))]:
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                return SchedulerTick(reason, interrupted, (), 0, self._remaining_count([]))
            try:
                preference_updates.append(preferences.apply(access, candidate_id, scheduler_token=scheduler_token, deadline=deadline))
            except MemoryFailure as error:
                if error.code in {'stale_source', 'candidate_not_found'}:
                    with self.runtime._connection() as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.workflow._scheduler_fence(db, scheduler_token)
                        db.execute("UPDATE preference_update_receipts_v1 SET state='source_unavailable' WHERE candidate_id=?", (candidate_id,))
                        db.execute('COMMIT')
                    continue
                if error.code in {'scheduler_foreground', 'scheduler_paused', 'scheduler_time_budget'}:
                    return SchedulerTick(reason, error.code.removeprefix('scheduler_'), (), 0, self._remaining_count([]))
                raise
        # A prior window may have committed assets just before losing its progress write.
        # Recover those exact durable source sets before a new arrival can change grouping.
        for run_id in completed_runs:
            run = self.workflow.status(access, run_id)
            coverage = self.workflow.recoverable_sources(access, run_id) if run.state == 'stale' else None
            if run.state != "succeeded" and not coverage:
                continue
            matching = set(source_ids).intersection(run.source_ids if coverage is None else coverage)
            if not matching:
                continue
            source_ids = [source for source in source_ids if source not in matching]
            if len(run_ids) >= runs_limit:
                continue  # Already-durable sources remain pending for the next bounded tick.
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                return SchedulerTick(reason, interrupted, tuple(run_ids), 0, self._remaining_count([]))
            if coverage is None:
                try:
                    result = self.workflow.result(access, run_id)
                except MemoryFailure as error:
                    if error.code != "incomplete_consolidation":
                        raise
                    result = self.workflow.recover(access, run_id, guarded_provider, scheduler_token=scheduler_token)
                coverage = {source: 'consolidated' if result.drafts else 'skipped:no_drafts' for source in matching}
            progress.extend((source, run_id, coverage[source]) for source in sorted(matching))
            run_ids.append(run_id)
        for index in range(0, len(source_ids), sources_limit):
            if len(run_ids) >= runs_limit:
                break
            interrupted = self._interruption(deadline, scheduler_token)
            if interrupted:
                return SchedulerTick(reason, interrupted, tuple(run_ids), 0, self._remaining_count([]))
            batch = tuple(source_ids[index:index + sources_limit])
            lease = self.workflow.begin(access, batch, processor_version="consolidation-scheduler-v1",
                lease_seconds=math.ceil(settings.max_trigger_seconds) + 30)
            run_id = lease.status.run_id
            try:
                if lease.token is None:
                    try:
                        result = self.workflow.result(access, run_id)
                    except MemoryFailure as error:
                        if error.code != "incomplete_consolidation":
                            raise
                        result = self.workflow.recover(access, run_id, guarded_provider, scheduler_token=scheduler_token)
                    progress.extend((message_id, run_id, "consolidated" if result.drafts else "skipped:no_drafts")
                                    for message_id in batch)
                    run_ids.append(run_id)
                    continue
                drafts = guarded_provider(lease)
                for draft in drafts:
                    if draft.kind not in CONSOLIDATION_KINDS:
                        raise MemoryFailure("invalid_consolidation_draft")
                if drafts:
                    self.workflow.complete(access, run_id, lease.token, drafts, scheduler_token=scheduler_token)
                    progress.extend((message_id, run_id, "consolidated") for message_id in batch)
                else:
                    # An empty draft set is a legitimate outcome: end the run with no candidates
                    # so an all-filtered chunk cannot crash-loop on invalid_consolidation_batch.
                    self.workflow.complete(access, run_id, lease.token, (), scheduler_token=scheduler_token)
                    progress.extend((message_id, run_id, "skipped:no_drafts") for message_id in batch)
                run_ids.append(run_id)
            except Exception as error:
                yielded = {"scheduler_foreground": "foreground", "scheduler_paused": "paused", "scheduler_time_budget": "time_budget"}.get(
                    error.code if isinstance(error, MemoryFailure) else "")
                try:
                    # Provider bugs raise non-domain exceptions too; an unfailed lease would stay
                    # dangling until expiry, so every in-process failure terminates the run.
                    if lease.token is not None:
                        self.workflow.fail(access, run_id, lease.token, error_code="timed_out" if yielded == "time_budget"
                            else "canceled" if yielded else "extractor_failed")
                except MemoryFailure:
                    pass  # An already-succeeded run cannot fail; replay records progress instead.
                if yielded:
                    return SchedulerTick(reason, yielded, tuple(run_ids), 0, self._remaining_count([]))
                raise
        processed = self._record_progress(progress, scheduler_token)
        return SchedulerTick(reason, None, tuple(run_ids), processed, self._remaining_count([]))

    def _interruption(self, deadline: float, scheduler_token: str) -> str | None:
        if time.monotonic() >= deadline:
            return "time_budget"
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            lease = db.execute("SELECT token,expires FROM scheduler_lease_v1 WHERE id=1").fetchone()
            if lease is None or lease[0] != scheduler_token or float(lease[1]) <= time.time():
                return "time_budget"
            if self._settings(db).paused:
                return "paused"
            if self._foreground_count(db):
                return "foreground"
            return "time_budget" if time.monotonic() >= deadline else None

    def _record_progress(self, progress: list[tuple[str, str, str]], scheduler_token: str) -> int:
        # Keep the whole selected window stable until every asset is durable. Partial progress
        # would shift the next scan boundary and give overlapping sources a different run key.
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.workflow._scheduler_fence(db, scheduler_token)
            before = db.total_changes
            for message_id, run_id, state in progress:
                db.execute("""INSERT INTO scheduler_progress_v1 VALUES (?,?,?,?)
                    ON CONFLICT(message_id) DO NOTHING""",
                    (message_id, run_id, state, datetime.now(timezone.utc).isoformat()))
            count = db.total_changes - before
            db.execute("COMMIT")
            return count

    def _remaining_count(self, leftover: list[str]) -> int:
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            return self._remaining(db, extra=tuple(leftover))

    @staticmethod
    def _remaining(db: sqlite3.Connection, extra: tuple[str, ...] = ()) -> int:
        placeholders = ",".join("?" for _ in extra) or "''"
        row = db.execute(f"""SELECT count(*) FROM messages JOIN jobs ON jobs.message_id=messages.id
            WHERE {_PENDING_SOURCES} AND messages.id NOT IN ({placeholders})""", extra).fetchone()
        return int(row[0])
