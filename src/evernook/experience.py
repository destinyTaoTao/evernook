"""Source-backed experience drafts and trusted version-bound review. Never an executor."""
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


@dataclass(frozen=True)
class ExperienceDraft:
    kind: str
    lesson: str = field(repr=False)
    conditions: str = field(repr=False)
    sources: tuple[EvidenceSpan, ...]
    disputed: bool = False
    processor_version: str = "trusted-proposal-v1"


@dataclass(frozen=True)
class ExperienceDecision:
    request_id: str
    experience_id: str
    expected_revision: int
    action: str
    draft: ExperienceDraft | None = None
    confirmation_id: str | None = None


@dataclass(frozen=True)
class ExperienceRecord:
    experience_id: str
    revision: int
    content_revision: int
    state: str
    draft: ExperienceDraft
    recorded_at: str
    confirmation_id: str | None
    confirmation_version: int | None
    request_id: str


@dataclass(frozen=True)
class ExperienceView:
    experience_id: str
    revision: int
    content_revision: int
    state: str
    record: ExperienceRecord | None
    error_code: str | None
    execution_authority: bool = False


class ExperienceRegistry:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS experience_events_v1 (
            request_id TEXT PRIMARY KEY, experience_id TEXT NOT NULL, revision INTEGER NOT NULL,
            fingerprint BLOB NOT NULL, record TEXT NOT NULL, UNIQUE(experience_id,revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS experience_approvals_v1 (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), experience_id TEXT NOT NULL,
            reviewed_revision INTEGER NOT NULL, action TEXT NOT NULL)""")

    @staticmethod
    def _record(raw: str) -> ExperienceRecord:
        value = json.loads(raw)
        value["draft"]["sources"] = tuple(EvidenceSpan(**item) for item in value["draft"]["sources"])
        value["draft"] = ExperienceDraft(**value["draft"])
        return ExperienceRecord(**value)

    @staticmethod
    def _latest(db: sqlite3.Connection, experience_id: str) -> ExperienceRecord | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_events_v1'").fetchone():
            return None
        row = db.execute("SELECT record FROM experience_events_v1 WHERE experience_id=? ORDER BY revision DESC LIMIT 1", (experience_id,)).fetchone()
        return None if row is None else ExperienceRegistry._record(row[0])

    @staticmethod
    def _validate(draft: ExperienceDraft) -> None:
        if (not isinstance(draft, ExperienceDraft) or not isinstance(draft.kind, str)
                or draft.kind not in {"lesson", "rule_draft", "skill_draft"}
                or not isinstance(draft.lesson, str) or not 1 <= len(draft.lesson.strip()) <= 4096 or len(draft.lesson) > 4096
                or not isinstance(draft.conditions, str) or not 1 <= len(draft.conditions.strip()) <= 2048 or len(draft.conditions) > 2048
                or type(draft.disputed) is not bool or not isinstance(draft.sources, tuple) or not 1 <= len(draft.sources) <= 16):
            raise MemoryFailure("invalid_experience_draft")
        _identifier(draft.processor_version)
        try:
            (draft.lesson + draft.conditions).encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if credential_signal(draft.lesson) or credential_signal(draft.conditions):
            raise MemoryFailure("protected_experience")
        for span in draft.sources:
            if not isinstance(span, EvidenceSpan):
                raise MemoryFailure("invalid_experience_source")
            _identifier(span.message_id)
            if (type(span.state_version) is not int or span.state_version < 1
                    or type(span.content_version) is not int or span.content_version < 1 or span.representation != "original"
                    or type(span.start) is not int or type(span.end) is not int or not 0 <= span.start < span.end
                    or span.end - span.start > 16384 or not isinstance(span.expected_text, str)
                    or len(span.expected_text) != span.end - span.start):
                raise MemoryFailure("invalid_experience_source")
            try:
                span.expected_text.encode("utf-8", errors="strict")
            except UnicodeError:
                raise MemoryFailure("invalid_encoding") from None

    def _sources(self, db: sqlite3.Connection, draft: ExperienceDraft) -> None:
        for span in draft.sources:
            state = self.runtime._state(db, span.message_id)
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (span.message_id,)).fetchone()
            message = self.runtime._stored(row)
            if (state.status != "active" or state.version != span.state_version
                    or message.receipt.content_version != span.content_version or not message.receipt.original_saved):
                raise MemoryFailure("stale_experience_source")
            # Failed/canceled tool evidence can support a failure lesson. Its completion is never rewritten.
            if message.event.role not in {"user", "assistant", "tool"}:
                raise MemoryFailure("invalid_experience_source")
            if message.event.text[span.start:span.end] != span.expected_text:
                raise MemoryFailure("experience_span_mismatch")

    def _confirmation(self, db: sqlite3.Connection, message_id: str) -> int:
        state = self.runtime._state(db, message_id)
        row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
        message = self.runtime._stored(row)
        if (state.status != "active" or message.event.role != "user" or message.event.completion != "complete"
                or not message.receipt.original_saved or not message.event.text.strip()):
            raise MemoryFailure("experience_user_confirmation_required")
        return state.version

    def decide(self, access: Access, request: ExperienceDecision) -> ExperienceRecord:
        """Only a trusted local caller may establish actual user intent; never expose as an LLM tool."""
        self.runtime._require_scope(access, "accept")
        if (not isinstance(request, ExperienceDecision) or type(request.expected_revision) is not int or request.expected_revision < 0
                or not isinstance(request.action, str) or request.action not in {"propose", "revise", "enable", "reject", "disable"}):
            raise MemoryFailure("invalid_experience_decision")
        for identifier in (request.request_id, request.experience_id):
            _identifier(identifier)
        if request.action in {"propose", "revise"}:
            if request.draft is None or request.confirmation_id is not None:
                raise MemoryFailure("invalid_experience_decision")
            self._validate(request.draft)
        else:
            if request.draft is not None or request.confirmation_id is None:
                raise MemoryFailure("experience_user_confirmation_required")
            _identifier(request.confirmation_id)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,record FROM experience_events_v1 WHERE request_id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("experience_request_conflict")
                repeated = self._record(existing["record"])
                self._sources(db, repeated.draft)
                if repeated.state == "enabled":
                    assert repeated.confirmation_id is not None
                    if self._confirmation(db, repeated.confirmation_id) != repeated.confirmation_version:
                        raise MemoryFailure("experience_user_confirmation_required")
                return repeated
            previous = self._latest(db, request.experience_id)
            revision = 0 if previous is None else previous.revision
            if revision != request.expected_revision:
                raise MemoryFailure("experience_revision_conflict")
            if (request.action == "propose") != (previous is None):
                raise MemoryFailure("invalid_experience_transition")
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
                approval = db.execute("SELECT experience_id,reviewed_revision,action FROM experience_approvals_v1 WHERE message_id=?", (request.confirmation_id,)).fetchone()
                binding = (request.experience_id, request.expected_revision, request.action)
                if approval is not None and tuple(approval) != binding:
                    raise MemoryFailure("experience_confirmation_conflict")
                received = db.execute("SELECT received FROM messages WHERE id=?", (request.confirmation_id,)).fetchone()[0]
                if _time(received) < _time(previous.recorded_at):
                    raise MemoryFailure("experience_confirmation_predates_review")
                if request.action == "enable":
                    if previous.state not in {"draft", "disabled"}:
                        raise MemoryFailure("invalid_experience_transition")
                    if draft.disputed:
                        raise MemoryFailure("experience_disputed")
                    self._sources(db, draft)
                    state = "enabled"
                elif request.action == "reject":
                    if previous.state != "draft":
                        raise MemoryFailure("invalid_experience_transition")
                    state = "rejected"
                else:
                    if previous.state != "enabled":
                        raise MemoryFailure("invalid_experience_transition")
                    state = "disabled"
                db.execute("INSERT OR IGNORE INTO experience_approvals_v1 VALUES (?,?,?,?)", (request.confirmation_id, *binding))
            record = ExperienceRecord(request.experience_id, revision + 1, content_revision, state, draft,
                datetime.now(timezone.utc).isoformat(), request.confirmation_id, confirmation, request.request_id)
            db.execute("INSERT INTO experience_events_v1 VALUES (?,?,?,?,?)", (request.request_id, request.experience_id, revision + 1, fingerprint, _json(asdict(record))))
            db.execute("COMMIT")
            return record

    def read(self, access: Access, experience_id: str) -> ExperienceView:
        self.runtime._require_scope(access, "recall")
        _identifier(experience_id)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            return self._view(db, experience_id)

    def _view(self, db: sqlite3.Connection, experience_id: str) -> ExperienceView:
        record = self._latest(db, experience_id)
        if record is None:
            raise MemoryFailure("experience_not_found")
        state, error = record.state, None
        try:
            self._sources(db, record.draft)
            if state == "enabled":
                assert record.confirmation_id is not None
                if self._confirmation(db, record.confirmation_id) != record.confirmation_version:
                    raise MemoryFailure("experience_user_confirmation_required")
            if record.draft.disputed:
                state = "disputed"
        except MemoryFailure as failure:
            if failure.code not in {"stale_experience_source", "message_not_found", "experience_span_mismatch", "experience_user_confirmation_required"}:
                raise
            state, error = "source_unavailable", failure.code
        return ExperienceView(experience_id, record.revision, record.content_revision, state, None if error else record, error)

    def propose_navigation(self, access: Access, *, request_id: str, experience_id: str, expected_revision: int,
                           policy_id: str, bucket: str, conditions: str) -> ExperienceRecord:
        from .maintenance import NavigationMaintenance

        view = NavigationMaintenance(self.runtime).read(access, policy_id, bucket)
        if view.summary is None or not view.summary.quotes:
            raise MemoryFailure("experience_navigation_unavailable")
        selected: list[EvidenceSpan] = []
        lesson = "来源摘录，待用户审查与归纳：\n"
        for quote in view.summary.quotes:
            line = quote.role + ": " + quote.source.expected_text + "\n"
            if len(selected) == 16 or len(lesson) + len(line) > 4096:
                break
            selected.append(quote.source)
            lesson += line
        if not selected:
            raise MemoryFailure("experience_navigation_unavailable")
        draft = ExperienceDraft("lesson", lesson, conditions, tuple(selected), processor_version="extractive-navigation-v1")
        return self.decide(access, ExperienceDecision(request_id, experience_id, expected_revision,
                           "propose" if expected_revision == 0 else "revise", draft))
