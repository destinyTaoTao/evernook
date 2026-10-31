"""Bounded, resumable extractive navigation. No model, fact acceptance or OS scheduler."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import sqlite3
from threading import Event as Signal
import time

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal


@dataclass(frozen=True)
class NavigationPolicy:
    offset_minutes: int
    topics: tuple[str, ...] = ()
    max_quotes: int = 32
    excerpt_chars: int = 512

    def validate(self) -> None:
        if (type(self.offset_minutes) is not int or not -840 <= self.offset_minutes <= 840
                or type(self.max_quotes) is not int or not 1 <= self.max_quotes <= 64
                or type(self.excerpt_chars) is not int or not 16 <= self.excerpt_chars <= 512
                or not isinstance(self.topics, tuple) or len(self.topics) > 8):
            raise MemoryFailure("invalid_navigation_policy")
        for topic in self.topics:
            _identifier(topic)
            if not topic.strip() or len(topic) > 64:
                raise MemoryFailure("invalid_navigation_policy")
        if len(set(self.topics)) != len(self.topics):
            raise MemoryFailure("invalid_navigation_policy")

    @property
    def policy_id(self) -> str:
        self.validate()
        return hashlib.sha256(_json(["extractive-navigation-v1", asdict(self)]).encode()).hexdigest()


@dataclass(frozen=True)
class MaintenanceBudget:
    max_messages: int
    max_jobs: int
    timeout: float

    def validate(self) -> None:
        if (type(self.max_messages) is not int or not 1 <= self.max_messages <= 256
                or type(self.max_jobs) is not int or not 1 <= self.max_jobs <= 16
                or type(self.timeout) not in (int, float) or not 0 < self.timeout <= 5):
            raise MemoryFailure("invalid_maintenance_budget")


@dataclass(frozen=True)
class NavigationQuote:
    source: EvidenceSpan
    role: str
    occurred_at: str | None
    received_at: str
    interpretation: str = "untrusted_source_quote"


@dataclass(frozen=True)
class NavigationSummary:
    bucket: str
    revision: int
    quotes: tuple[NavigationQuote, ...]
    total_sources: int
    omitted_sources: int
    generated_at: str
    scanned_message_row: int
    scanned_change_row: int
    processor: str = "extractive-navigation-v1"
    time_basis: str = "received_at_fixed_offset"
    authority: str = "navigation_only"


@dataclass(frozen=True)
class NavigationView:
    bucket: str
    revision: int
    state: str
    attempt: int
    error_code: str | None
    summary: NavigationSummary | None


@dataclass(frozen=True)
class MaintenanceStatus:
    policy_id: str
    paused: bool
    message_cursor: int
    change_cursor: int
    observed_message_head: int
    observed_change_head: int
    queued: int
    running: int
    succeeded: int
    failed: int
    buckets: tuple[str, ...]
    more_buckets: bool


@dataclass(frozen=True)
class MaintenanceTick:
    discovered_messages: int
    discovered_changes: int
    completed_jobs: int
    stop_reason: str
    status: MaintenanceStatus


@dataclass(frozen=True)
class _JobLease:
    bucket: str
    revision: int
    epoch: int
    token: str
    policy: NavigationPolicy


class NavigationMaintenance:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS navigation_policies_v1 (
            id TEXT PRIMARY KEY, settings TEXT NOT NULL, paused INTEGER NOT NULL,
            epoch INTEGER NOT NULL, message_cursor INTEGER NOT NULL, change_cursor INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS navigation_jobs_v1 (
            policy TEXT NOT NULL REFERENCES navigation_policies_v1(id), bucket TEXT NOT NULL,
            revision INTEGER NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL,
            lease TEXT, lease_until REAL, result TEXT, error_code TEXT,
            PRIMARY KEY(policy,bucket))""")
        db.execute("""CREATE TABLE IF NOT EXISTS navigation_sources_v1 (
            policy TEXT NOT NULL, bucket TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages(id),
            source_row INTEGER NOT NULL, PRIMARY KEY(policy,bucket,message_id),
            FOREIGN KEY(policy,bucket) REFERENCES navigation_jobs_v1(policy,bucket))""")
        db.execute("CREATE INDEX IF NOT EXISTS navigation_source_changes_v1 ON navigation_sources_v1(policy,message_id)")
        db.execute("CREATE INDEX IF NOT EXISTS navigation_source_order_v1 ON navigation_sources_v1(policy,bucket,source_row DESC)")

    @staticmethod
    def _policy(db: sqlite3.Connection, policy_id: str) -> sqlite3.Row:
        _identifier(policy_id)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='navigation_policies_v1'").fetchone():
            raise MemoryFailure("navigation_policy_not_found")
        row = db.execute("SELECT * FROM navigation_policies_v1 WHERE id=?", (policy_id,)).fetchone()
        if row is None:
            raise MemoryFailure("navigation_policy_not_found")
        assert isinstance(row, sqlite3.Row)
        return row

    @staticmethod
    def _decode_policy(row: sqlite3.Row) -> NavigationPolicy:
        value = json.loads(row["settings"])
        value["topics"] = tuple(value["topics"])
        policy = NavigationPolicy(**value)
        policy.validate()
        return policy

    def configure(self, access: Access, policy: NavigationPolicy) -> MaintenanceStatus:
        self.runtime._require_scope(access, "maintain")
        if not isinstance(policy, NavigationPolicy):
            raise MemoryFailure("invalid_navigation_policy")
        policy_id = policy.policy_id
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            db.execute("INSERT OR IGNORE INTO navigation_policies_v1 VALUES (?,?,0,0,0,0)", (policy_id, _json(asdict(policy))))
            db.execute("COMMIT")
        return self.status(access, policy_id)

    def pause(self, access: Access, policy_id: str, *, paused: bool) -> MaintenanceStatus:
        self.runtime._require_scope(access, "maintain")
        if type(paused) is not bool:
            raise MemoryFailure("invalid_maintenance_pause")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._policy(db, policy_id)
            db.execute("UPDATE navigation_policies_v1 SET paused=?,epoch=epoch+1 WHERE id=?", (int(paused), policy_id))
            # A worker holding an older epoch cannot publish after either pause or resume.
            db.execute("UPDATE navigation_jobs_v1 SET state='queued',lease=NULL,lease_until=NULL WHERE policy=? AND state='running'", (policy_id,))
            db.execute("COMMIT")
        return self.status(access, policy_id)

    @staticmethod
    def _buckets(policy: NavigationPolicy, received: str, text: str) -> tuple[str, ...]:
        local = datetime.fromisoformat(received).astimezone(timezone(timedelta(minutes=policy.offset_minutes)))
        week = local.date() - timedelta(days=local.weekday())
        return ("day:" + local.date().isoformat(), "week:" + week.isoformat(),
                *("topic:" + topic for topic in policy.topics if topic.casefold() in text.casefold()))

    def _discover(self, access: Access, policy_id: str, maximum: int, stop: Callable[[], str | None]) -> tuple[int, int]:
        self.runtime._authorize(access)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._policy(db, policy_id)
            if row["paused"]:
                return 0, 0
            policy = self._decode_policy(row)
            touched: set[str] = set()
            messages = db.execute("SELECT rowid AS source_row,id,event,receipt,received FROM messages WHERE rowid>? ORDER BY rowid LIMIT ?",
                                  (row["message_cursor"], maximum))
            scanned = 0
            cursor = row["message_cursor"]
            for message in messages:
                if stop():
                    break
                scanned += 1
                cursor = message["source_row"]
                event, receipt = json.loads(message["event"]), json.loads(message["receipt"])
                if (event["role"] not in {"user", "assistant", "tool"} or event["completion"] != "complete"
                        or not receipt["original_saved"] or not event["text"]):
                    continue
                for bucket in self._buckets(policy, message["received"], event["text"]):
                    db.execute("INSERT OR IGNORE INTO navigation_jobs_v1 VALUES (?,?,0,'queued',0,NULL,NULL,NULL,NULL)", (policy_id, bucket))
                    db.execute("INSERT OR IGNORE INTO navigation_sources_v1 VALUES (?,?,?,?)", (policy_id, bucket, message["id"], message["source_row"]))
                    touched.add(bucket)
            changes = [] if stop() else db.execute("SELECT rowid AS change_row,target_id FROM changes WHERE rowid>? ORDER BY rowid LIMIT ?",
                                 (row["change_cursor"], maximum)).fetchall()
            for change in changes:
                touched.update(str(item[0]) for item in db.execute("SELECT bucket FROM navigation_sources_v1 WHERE policy=? AND message_id=?", (policy_id, change["target_id"])))
            for bucket in touched:
                db.execute("UPDATE navigation_jobs_v1 SET revision=revision+1,state='queued',lease=NULL,lease_until=NULL,result=NULL,error_code=NULL WHERE policy=? AND bucket=?", (policy_id, bucket))
            db.execute("UPDATE navigation_policies_v1 SET message_cursor=?,change_cursor=? WHERE id=?",
                       (cursor, changes[-1]["change_row"] if changes else row["change_cursor"], policy_id))
            db.execute("COMMIT")
            return scanned, len(changes)

    def _claim(self, access: Access, policy_id: str) -> _JobLease | None:
        self.runtime._authorize(access)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._policy(db, policy_id)
            if row["paused"]:
                return None
            job = db.execute("""SELECT * FROM navigation_jobs_v1 WHERE policy=? AND
                (state IN ('queued','failed') OR (state='running' AND lease_until<=?))
                ORDER BY attempt,bucket LIMIT 1""", (policy_id, time.time())).fetchone()
            if job is None:
                return None
            policy = self._decode_policy(row)
            bucket, revision = str(job["bucket"]), int(job["revision"])
            lease = secrets.token_hex(32)
            db.execute("UPDATE navigation_jobs_v1 SET state='running',attempt=attempt+1,lease=?,lease_until=?,error_code=NULL WHERE policy=? AND bucket=?",
                       (lease, time.time() + 30, policy_id, bucket))
            db.execute("COMMIT")
            return _JobLease(bucket, revision, int(row["epoch"]), lease, policy)

    def _summarize(self, policy_id: str, claim: _JobLease) -> NavigationSummary:
        policy, bucket = claim.policy, claim.bucket
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            progress = self._policy(db, policy_id)
            # Aggregate reads use a WAL snapshot, outside the claim's write transaction.
            eligible = """FROM navigation_sources_v1 s JOIN messages ON messages.id=s.message_id
                LEFT JOIN visibility v ON v.message_id=messages.id WHERE s.policy=? AND s.bucket=?
                AND COALESCE(v.status,'active')='active' AND json_extract(messages.receipt,'$.original_saved')=1"""
            total = int(db.execute("SELECT count(*) " + eligible, (policy_id, bucket)).fetchone()[0])
            selected = db.execute("""SELECT messages.id,COALESCE(v.version,1) AS version,messages.receipt,
                json_extract(messages.event,'$.role') AS role,
                json_extract(messages.event,'$.occurred_at') AS occurred_at,
                substr(json_extract(messages.event,'$.text'),1,?) AS excerpt """ + eligible +
                " ORDER BY s.source_row DESC LIMIT ?", (policy.excerpt_chars, policy_id, bucket, policy.max_quotes))
            quotes = []
            for item in selected:
                receipt = json.loads(item["receipt"])
                text = str(item["excerpt"])
                if not text or credential_signal(text):
                    continue
                quotes.append(NavigationQuote(EvidenceSpan(item["id"], int(item["version"]), receipt["representation"], receipt["content_version"], 0, len(text), text),
                                              item["role"], item["occurred_at"], receipt["received_at"]))
            return NavigationSummary(bucket, claim.revision, tuple(reversed(quotes)), total, total - len(quotes), datetime.now(timezone.utc).isoformat(),
                                     int(progress["message_cursor"]), int(progress["change_cursor"]))

    def _current(self, db: sqlite3.Connection, policy_id: str, summary: NavigationSummary) -> bool:
        if db.execute("""SELECT 1 FROM changes c JOIN navigation_sources_v1 s ON s.message_id=c.target_id
            WHERE s.policy=? AND s.bucket=? AND c.rowid>? LIMIT 1""", (policy_id, summary.bucket, summary.scanned_change_row)).fetchone():
            return False
        for quote in summary.quotes:
            span = quote.source
            try:
                state = self.runtime._state(db, span.message_id)
            except MemoryFailure as error:
                if error.code != "message_not_found":
                    raise
                return False
            if state.status != "active" or state.version != span.state_version:
                return False
            row = db.execute("SELECT receipt FROM messages WHERE id=?", (span.message_id,)).fetchone()
            receipt = json.loads(row[0])
            if not receipt["original_saved"] or receipt["content_version"] != span.content_version:
                return False
        return True

    def _finish(self, access: Access, policy_id: str, claim: _JobLease, summary: NavigationSummary | None, error: str | None) -> bool:
        self.runtime._authorize(access)
        bucket, revision, epoch, lease = claim.bucket, claim.revision, claim.epoch, claim.token
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = self._policy(db, policy_id)
            row = db.execute("SELECT * FROM navigation_jobs_v1 WHERE policy=? AND bucket=?", (policy_id, bucket)).fetchone()
            if (policy["paused"] or int(policy["epoch"]) != epoch or row["revision"] != revision
                    or row["state"] != "running" or row["lease"] != lease or float(row["lease_until"]) <= time.time()):
                return False
            if error is None and (summary is None or not self._current(db, policy_id, summary)):
                error = "stale_source"
            payload = None
            if error is None:
                assert summary is not None
                payload = _json(asdict(summary))
            # Only genuine failure classes carry error_code; cooperative stops (cancel/foreground/time
            # budget) requeue healthily so a routine yield is never misread as a failure category.
            db.execute("UPDATE navigation_jobs_v1 SET state=?,lease=NULL,lease_until=NULL,result=?,error_code=? WHERE policy=? AND bucket=?",
                       ("succeeded" if error is None else "failed" if error == "processor_failed" else "queued", payload,
                        error if error in ("processor_failed", "stale_source") else None, policy_id, bucket))
            db.execute("COMMIT")
            return error is None

    def tick(self, access: Access, policy_id: str, *, budget: MaintenanceBudget,
             cancel: Signal | None = None, foreground: Signal | None = None) -> MaintenanceTick:
        self.runtime._require_scope(access, "maintain")
        if not isinstance(budget, MaintenanceBudget):
            raise MemoryFailure("invalid_maintenance_budget")
        budget.validate()
        deadline = time.monotonic() + budget.timeout

        def stop() -> str | None:
            if cancel is not None and cancel.is_set():
                return "canceled"
            if foreground is not None and foreground.is_set():
                return "foreground"
            if time.monotonic() >= deadline:
                return "time_budget"
            return None

        reason = stop()
        discovered, changed = (0, 0) if reason else self._discover(access, policy_id, budget.max_messages, stop)
        completed = 0
        while reason is None and completed < budget.max_jobs:
            reason = stop()
            if reason:
                break
            claim = self._claim(access, policy_id)
            if claim is None:
                reason = "idle_or_paused"
                break
            reason = stop()
            summary = None
            try:
                if reason is None:
                    summary = self._summarize(policy_id, claim)
                    reason = stop()
            except Exception:
                self._finish(access, policy_id, claim, None, "processor_failed")
                reason = "processor_failed"
                break
            if not self._finish(access, policy_id, claim, summary, reason):
                reason = reason or "state_changed"
                break
            completed += 1
        return MaintenanceTick(discovered, changed, completed, reason or "job_budget", self.status(access, policy_id))

    def status(self, access: Access, policy_id: str, *, after_bucket: str = "") -> MaintenanceStatus:
        self.runtime._require_scope(access, "recall")
        if not isinstance(after_bucket, str) or len(after_bucket) > 256:
            raise MemoryFailure("invalid_navigation_cursor")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            row = self._policy(db, policy_id)
            counts = {str(item[0]): int(item[1]) for item in db.execute("SELECT state,count(*) FROM navigation_jobs_v1 WHERE policy=? GROUP BY state", (policy_id,))}
            buckets = tuple(str(item[0]) for item in db.execute("SELECT bucket FROM navigation_jobs_v1 WHERE policy=? AND bucket>? ORDER BY bucket LIMIT 65", (policy_id, after_bucket)))
            return MaintenanceStatus(policy_id, bool(row["paused"]), int(row["message_cursor"]), int(row["change_cursor"]),
                int(db.execute("SELECT COALESCE(max(rowid),0) FROM messages").fetchone()[0]), int(db.execute("SELECT COALESCE(max(rowid),0) FROM changes").fetchone()[0]),
                counts.get("queued", 0), counts.get("running", 0), counts.get("succeeded", 0), counts.get("failed", 0), buckets[:64], len(buckets) > 64)

    def read(self, access: Access, policy_id: str, bucket: str) -> NavigationView:
        self.runtime._require_scope(access, "recall")
        _identifier(bucket)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self._policy(db, policy_id)
            row = db.execute("SELECT * FROM navigation_jobs_v1 WHERE policy=? AND bucket=?", (policy_id, bucket)).fetchone()
            if row is None:
                raise MemoryFailure("navigation_not_found")
            summary = None
            state = str(row["state"])
            if state == "succeeded":
                value = json.loads(row["result"])
                value["quotes"] = tuple(NavigationQuote(source=EvidenceSpan(**quote.pop("source")), **quote) for quote in value["quotes"])
                candidate = NavigationSummary(**value)
                if self._current(db, policy_id, candidate):
                    summary = candidate
                else:
                    state = "stale"
            if state == "running" and float(row["lease_until"]) <= time.time():
                state = "expired"
            return NavigationView(bucket, int(row["revision"]), state, int(row["attempt"]), "stale_source" if state == "stale" else row["error_code"], summary)
