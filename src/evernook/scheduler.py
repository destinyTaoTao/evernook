"""In-process consolidation scheduling: session-end and idle triggers, pause/resume, crash-safe resume."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3

from .candidates import CandidateLease
from .consolidation import CONSOLIDATION_KINDS, ConsolidationDraft, ConsolidationWorkflow
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _RECALLABLE


SCHEDULER_REASONS = ("session_end", "idle", "manual")
_SCHEDULER_DEFAULT_SOURCES = 8
_SCHEDULER_DEFAULT_RUNS = 1


@dataclass(frozen=True)
class SchedulerStatus:
    paused: bool
    max_sources_per_run: int
    max_runs_per_trigger: int
    processed: int
    session_marks: int
    skip_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SchedulerTick:
    reason: str
    skipped: str | None
    run_ids: tuple[str, ...]
    processed: int
    remaining: int


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
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_progress_v1 (
            message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL, processed_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS scheduler_sessions_v1 (
            entry TEXT NOT NULL, session TEXT NOT NULL, marked_at TEXT NOT NULL,
            PRIMARY KEY(entry,session,marked_at))""")

    def _settings(self, db: sqlite3.Connection) -> tuple[bool, int, int]:
        row = db.execute("SELECT paused,max_sources_per_run,max_runs_per_trigger FROM scheduler_settings_v1 WHERE id=1").fetchone()
        if row is None:
            return False, _SCHEDULER_DEFAULT_SOURCES, _SCHEDULER_DEFAULT_RUNS
        return bool(row["paused"]), int(row["max_sources_per_run"]), int(row["max_runs_per_trigger"])

    def configure(self, access: Access, *, paused: bool | None = None, max_sources_per_run: int | None = None,
                  max_runs_per_trigger: int | None = None) -> SchedulerStatus:
        self.runtime._require_scope(access, "maintain", "scheduler_configure")
        for value in (max_sources_per_run, max_runs_per_trigger):
            if value is not None and (type(value) is not int or not 1 <= value <= 16):
                raise MemoryFailure("invalid_scheduler_config")
        if paused is not None and type(paused) is not bool:
            raise MemoryFailure("invalid_scheduler_config")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            current_paused, sources, runs = self._settings(db)
            new_paused = current_paused if paused is None else paused
            new_sources = sources if max_sources_per_run is None else max_sources_per_run
            new_runs = runs if max_runs_per_trigger is None else max_runs_per_trigger
            db.execute("""INSERT INTO scheduler_settings_v1 VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                paused=excluded.paused, max_sources_per_run=excluded.max_sources_per_run,
                max_runs_per_trigger=excluded.max_runs_per_trigger""",
                (int(new_paused), new_sources, new_runs))
            db.execute("COMMIT")
        return self.status(access)

    def status(self, access: Access) -> SchedulerStatus:
        self.runtime._require_scope(access, "maintain", "scheduler_status")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self._schema(db)
            paused, sources, runs = self._settings(db)
            processed = int(db.execute("SELECT count(*) FROM scheduler_progress_v1").fetchone()[0])
            marks = int(db.execute("SELECT count(*) FROM scheduler_sessions_v1").fetchone()[0])
            reasons = {str(row["state"])[8:]: int(row["count"]) for row in db.execute(
                "SELECT state,count(*) AS count FROM scheduler_progress_v1 WHERE state LIKE 'skipped:%' GROUP BY state")}
            return SchedulerStatus(paused, sources, runs, processed, marks, reasons)

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
        """Bounded consolidation pass over unprocessed pending raw jobs; idempotent and crash-resumable.

        Trigger reasons are labels: the host owns trigger timing and session marks are observability
        bookkeeping, not a queue this method drains. Ineligible sources are individually skipped
        before batching; transient provider failures leave eligible evidence retryable.
        """
        self.runtime._require_scope(access, "maintain", "scheduler_trigger")
        if not isinstance(reason, str) or reason not in SCHEDULER_REASONS:
            raise MemoryFailure("invalid_scheduler_reason")
        if provider is not None and not callable(provider):
            raise MemoryFailure("invalid_scheduler_config")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self._schema(db)
            paused, sources_limit, runs_limit = self._settings(db)
            if paused:
                db.execute("COMMIT")
                return SchedulerTick(reason, "paused", (), 0, self._remaining(db))
            # Old versions skipped whole mixed batches without a reason. Recheck those sources;
            # new per-source reasons and verified empty results make this a one-time recovery.
            db.execute("DELETE FROM scheduler_progress_v1 WHERE state='skipped'")
            candidates = db.execute(f"""SELECT messages.id,messages.event,messages.receipt FROM messages JOIN jobs ON jobs.message_id=messages.id
                WHERE jobs.state='pending' AND messages.id NOT IN (SELECT message_id FROM scheduler_progress_v1)
                AND {_RECALLABLE} ORDER BY messages.received, messages.id LIMIT ?""",
                (sources_limit * runs_limit,)).fetchall()
            db.execute("COMMIT")
        source_ids: list[str] = []
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
            else:
                progress.append((str(row["id"]), "", "skipped:" + skip_reason))
        run_ids: list[str] = []
        for index in range(0, len(source_ids), sources_limit):
            if len(run_ids) >= runs_limit:
                break
            batch = tuple(source_ids[index:index + sources_limit])
            lease = self.workflow.begin(access, batch, processor_version="consolidation-scheduler-v1")
            run_id = lease.status.run_id
            if lease.token is None:
                try:
                    result = self.workflow.result(access, run_id)
                except MemoryFailure as error:
                    if error.code != "incomplete_consolidation":
                        raise
                    result = self.workflow.recover(access, run_id, provider or extractive_provider)
                progress.extend((message_id, run_id, "consolidated" if result.drafts else "skipped:no_drafts")
                                for message_id in batch)
                run_ids.append(run_id)
                continue
            try:
                drafts = provider(lease) if provider is not None else extractive_provider(lease)
                for draft in drafts:
                    if draft.kind not in CONSOLIDATION_KINDS:
                        raise MemoryFailure("invalid_consolidation_draft")
                if drafts:
                    self.workflow.complete(access, run_id, lease.token, drafts)
                    progress.extend((message_id, run_id, "consolidated") for message_id in batch)
                else:
                    # An empty draft set is a legitimate outcome: end the run with no candidates
                    # so an all-filtered chunk cannot crash-loop on invalid_consolidation_batch.
                    self.workflow.candidates.complete(access, run_id, lease.token, ())
                    progress.extend((message_id, run_id, "skipped:no_drafts") for message_id in batch)
                run_ids.append(run_id)
            except Exception:
                try:
                    # Provider bugs raise non-domain exceptions too; an unfailed lease would stay
                    # dangling until expiry, so every in-process failure terminates the run.
                    self.workflow.fail(access, run_id, lease.token, error_code="extractor_failed")
                except MemoryFailure:
                    pass  # An already-succeeded run cannot fail; replay records progress instead.
                raise
        processed = self._record_progress(progress)
        return SchedulerTick(reason, None, tuple(run_ids), processed, self._remaining_count([]))

    def _record_progress(self, progress: list[tuple[str, str, str]]) -> int:
        # Keep the whole selected window stable until every asset is durable. Partial progress
        # would shift the next scan boundary and give overlapping sources a different run key.
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
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
            WHERE jobs.state='pending' AND messages.id NOT IN (SELECT message_id FROM scheduler_progress_v1)
            AND messages.id NOT IN ({placeholders}) AND {_RECALLABLE}""", extra).fetchone()
        return int(row[0])
