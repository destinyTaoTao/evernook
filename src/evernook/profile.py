"""Source-backed user profile tags with draft→confirm lifecycle and aggregate views. No credential material."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3

from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json, _time
from .safety import credential_signal


PROFILE_CATEGORIES = ("identity", "preference", "interest", "relation", "goal", "boundary")
PROFILE_TAXONOMY_VERSION = 1


@dataclass(frozen=True)
class ProfileDraft:
    category: str
    label: str = field(repr=False)
    confidence: float
    sources: tuple[EvidenceSpan, ...]
    processor_version: str = "trusted-proposal-v1"


@dataclass(frozen=True)
class ProfileDecision:
    request_id: str
    tag_id: str
    expected_revision: int
    action: str
    draft: ProfileDraft | None = None
    confirmation_id: str | None = None


@dataclass(frozen=True)
class ProfileRecord:
    tag_id: str
    revision: int
    content_revision: int
    state: str
    taxonomy_version: int
    draft: ProfileDraft
    recorded_at: str
    confirmation_id: str | None
    confirmation_version: int | None
    request_id: str


@dataclass(frozen=True)
class ProfileView:
    tag_id: str
    revision: int
    content_revision: int
    state: str
    record: ProfileRecord | None
    error_code: str | None


@dataclass(frozen=True)
class ProfileSummary:
    taxonomy_version: int
    tags: tuple[ProfileRecord, ...]


@dataclass(frozen=True)
class ProfileHistoryPage:
    records: tuple[ProfileRecord, ...]
    next_offset: int | None


class ProfileRegistry:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS profile_events_v1 (
            request_id TEXT PRIMARY KEY, tag_id TEXT NOT NULL, revision INTEGER NOT NULL,
            fingerprint BLOB NOT NULL, record TEXT NOT NULL, UNIQUE(tag_id,revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS profile_approvals_v1 (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), tag_id TEXT NOT NULL,
            reviewed_revision INTEGER NOT NULL, action TEXT NOT NULL)""")

    @staticmethod
    def _record(raw: str) -> ProfileRecord:
        value = json.loads(raw)
        value["draft"]["sources"] = tuple(EvidenceSpan(**item) for item in value["draft"]["sources"])
        value["draft"] = ProfileDraft(**value["draft"])
        return ProfileRecord(**value)

    @staticmethod
    def _latest(db: sqlite3.Connection, tag_id: str) -> ProfileRecord | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
            return None
        row = db.execute("SELECT record FROM profile_events_v1 WHERE tag_id=? ORDER BY revision DESC LIMIT 1", (tag_id,)).fetchone()
        return None if row is None else ProfileRegistry._record(row[0])

    @staticmethod
    def _validate(draft: ProfileDraft) -> None:
        if (not isinstance(draft, ProfileDraft) or not isinstance(draft.category, str) or not 1 <= len(draft.category) <= 64
                or not isinstance(draft.label, str) or not 1 <= len(draft.label.strip()) <= 256 or len(draft.label) > 256
                or type(draft.confidence) is not float or not 0 <= draft.confidence <= 1
                or not isinstance(draft.sources, tuple) or not 1 <= len(draft.sources) <= 16):
            raise MemoryFailure("invalid_profile_draft")
        _identifier(draft.category)
        _identifier(draft.processor_version)
        try:
            draft.label.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if credential_signal(draft.label):
            raise MemoryFailure("protected_profile")
        for span in draft.sources:
            if not isinstance(span, EvidenceSpan):
                raise MemoryFailure("invalid_profile_source")
            _identifier(span.message_id)
            if (type(span.state_version) is not int or span.state_version < 1
                    or type(span.content_version) is not int or span.content_version < 1 or span.representation != "original"
                    or type(span.start) is not int or type(span.end) is not int or not 0 <= span.start < span.end
                    or span.end - span.start > 16384 or not isinstance(span.expected_text, str)
                    or len(span.expected_text) != span.end - span.start):
                raise MemoryFailure("invalid_profile_source")
            try:
                span.expected_text.encode("utf-8", errors="strict")
            except UnicodeError:
                raise MemoryFailure("invalid_encoding") from None

    def _sources(self, db: sqlite3.Connection, draft: ProfileDraft) -> None:
        for span in draft.sources:
            state = self.runtime._state(db, span.message_id)
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (span.message_id,)).fetchone()
            message = self.runtime._stored(row)
            if (state.status != "active" or state.version != span.state_version
                    or message.receipt.content_version != span.content_version or not message.receipt.original_saved):
                raise MemoryFailure("stale_profile_source")
            if message.event.role not in {"user", "assistant", "tool"}:
                raise MemoryFailure("invalid_profile_source")
            if message.event.text[span.start:span.end] != span.expected_text:
                raise MemoryFailure("profile_span_mismatch")

    def _confirmation(self, db: sqlite3.Connection, message_id: str) -> int:
        state = self.runtime._state(db, message_id)
        row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
        message = self.runtime._stored(row)
        if (state.status != "active" or message.event.role != "user" or message.event.completion != "complete"
                or not message.receipt.original_saved or not message.event.text.strip()):
            raise MemoryFailure("profile_user_confirmation_required")
        return state.version

    def decide(self, access: Access, request: ProfileDecision) -> ProfileRecord:
        """Only a trusted local caller may establish actual user intent; never expose as an LLM tool."""
        self.runtime._authorize(access)
        if (not isinstance(request, ProfileDecision) or type(request.expected_revision) is not int or request.expected_revision < 0
                or not isinstance(request.action, str)
                or request.action not in {"propose", "revise", "confirm", "reject", "suppress"}):
            raise MemoryFailure("invalid_profile_decision")
        for identifier in (request.request_id, request.tag_id):
            _identifier(identifier)
        if request.action in {"propose", "revise"}:
            if request.draft is None or request.confirmation_id is not None:
                raise MemoryFailure("invalid_profile_decision")
            self._validate(request.draft)
        else:
            if request.draft is not None or request.confirmation_id is None:
                raise MemoryFailure("profile_user_confirmation_required")
            _identifier(request.confirmation_id)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,record FROM profile_events_v1 WHERE request_id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("profile_request_conflict")
                repeated = self._record(existing["record"])
                self._sources(db, repeated.draft)
                if repeated.state == "confirmed":
                    assert repeated.confirmation_id is not None
                    if self._confirmation(db, repeated.confirmation_id) != repeated.confirmation_version:
                        raise MemoryFailure("profile_user_confirmation_required")
                return repeated
            previous = self._latest(db, request.tag_id)
            revision = 0 if previous is None else previous.revision
            if revision != request.expected_revision:
                raise MemoryFailure("profile_revision_conflict")
            if (request.action == "propose") != (previous is None):
                raise MemoryFailure("invalid_profile_transition")
            confirmation = None
            if request.draft is not None:
                draft = request.draft
                self._sources(db, draft)
                content_revision = 1 if previous is None else previous.content_revision + 1
                state = "draft"
            else:
                assert previous is not None and request.confirmation_id is not None
                draft, content_revision = previous.draft, previous.content_revision
                confirmation = self._confirmation(db, request.confirmation_id)
                approval = db.execute("SELECT tag_id,reviewed_revision,action FROM profile_approvals_v1 WHERE message_id=?", (request.confirmation_id,)).fetchone()
                binding = (request.tag_id, request.expected_revision, request.action)
                if approval is not None and tuple(approval) != binding:
                    raise MemoryFailure("profile_confirmation_conflict")
                received = db.execute("SELECT received FROM messages WHERE id=?", (request.confirmation_id,)).fetchone()[0]
                if _time(received) < _time(previous.recorded_at):
                    raise MemoryFailure("profile_confirmation_predates_review")
                if request.action == "confirm":
                    if previous.state not in {"draft", "suppressed"}:
                        raise MemoryFailure("invalid_profile_transition")
                    self._sources(db, draft)
                    state = "confirmed"
                elif request.action == "reject":
                    if previous.state != "draft":
                        raise MemoryFailure("invalid_profile_transition")
                    state = "rejected"
                else:
                    if previous.state != "confirmed":
                        raise MemoryFailure("invalid_profile_transition")
                    state = "suppressed"
                db.execute("INSERT OR IGNORE INTO profile_approvals_v1 VALUES (?,?,?,?)", (request.confirmation_id, *binding))
            record = ProfileRecord(request.tag_id, revision + 1, content_revision, state, PROFILE_TAXONOMY_VERSION,
                draft, datetime.now(timezone.utc).isoformat(), request.confirmation_id, confirmation, request.request_id)
            db.execute("INSERT INTO profile_events_v1 VALUES (?,?,?,?,?)",
                (request.request_id, request.tag_id, revision + 1, fingerprint, _json(asdict(record))))
            db.execute("COMMIT")
            return record

    def read(self, access: Access, tag_id: str) -> ProfileView:
        self.runtime._authorize(access)
        _identifier(tag_id)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            return self._view(db, tag_id)

    def _view(self, db: sqlite3.Connection, tag_id: str) -> ProfileView:
        record = self._latest(db, tag_id)
        if record is None:
            raise MemoryFailure("profile_not_found")
        state, error = record.state, None
        try:
            self._sources(db, record.draft)
            if state == "confirmed":
                assert record.confirmation_id is not None
                if self._confirmation(db, record.confirmation_id) != record.confirmation_version:
                    raise MemoryFailure("profile_user_confirmation_required")
        except MemoryFailure as failure:
            if failure.code not in {"stale_profile_source", "message_not_found", "profile_span_mismatch", "profile_user_confirmation_required"}:
                raise
            state, error = "source_unavailable", failure.code
        return ProfileView(tag_id, record.revision, record.content_revision, state, None if error else record, error)

    def summary(self, access: Access, *, categories: tuple[str, ...] | None = None,
                include_suppressed: bool = False) -> ProfileSummary:
        """Aggregate current tags; suppressed and rejected tags stay out of the default view."""
        self.runtime._authorize(access)
        if categories is not None:
            if (not isinstance(categories, tuple) or any(not isinstance(item, str) for item in categories)
                    or len(categories) != len(set(categories))):
                raise MemoryFailure("invalid_profile_summary")
            for item in categories:
                _identifier(item)
        if type(include_suppressed) is not bool:
            raise MemoryFailure("invalid_profile_summary")
        states = {"confirmed", "suppressed"} if include_suppressed else {"confirmed"}
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
                return ProfileSummary(PROFILE_TAXONOMY_VERSION, ())
            rows = db.execute("""SELECT record FROM profile_events_v1 e JOIN (
                SELECT tag_id, MAX(revision) AS revision FROM profile_events_v1 GROUP BY tag_id) head
                ON e.tag_id=head.tag_id AND e.revision=head.revision ORDER BY e.tag_id""").fetchall()
            tags: list[ProfileRecord] = []
            for row in rows:
                record = self._record(row["record"])
                if record.state not in states:
                    continue
                if categories is not None and record.draft.category not in categories:
                    continue
                view = self._view(db, record.tag_id)
                if view.state != "confirmed" and not (include_suppressed and view.state == "suppressed"):
                    continue
                assert view.record is not None
                tags.append(view.record)
            return ProfileSummary(PROFILE_TAXONOMY_VERSION, tuple(tags))

    def history(self, access: Access, tag_id: str, *, limit: int = 100, offset: int = 0) -> ProfileHistoryPage:
        self.runtime._authorize(access)
        _identifier(tag_id)
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_profile_summary")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
                return ProfileHistoryPage((), None)
            rows = db.execute("SELECT record FROM profile_events_v1 WHERE tag_id=? ORDER BY revision LIMIT ? OFFSET ?",
                (tag_id, limit + 1, offset)).fetchall()
            records = tuple(self._record(row["record"]) for row in rows[:limit])
            return ProfileHistoryPage(records, offset + limit if len(rows) > limit else None)
