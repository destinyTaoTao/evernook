"""Explicit local candidate workflow; no model, scheduler, or automatic fact acceptance."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from uuid import uuid4

from .models import Access, MemoryFailure, StoredMessage
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal


@dataclass(frozen=True)
class EvidenceSpan:
    message_id: str
    state_version: int
    representation: str
    content_version: int
    start: int
    end: int
    expected_text: str = field(repr=False)


@dataclass(frozen=True)
class CandidateDraft:
    text: str = field(repr=False)
    provenance: str
    sources: tuple[EvidenceSpan, ...]
    disputed: bool = False


@dataclass(frozen=True)
class FactCandidate:
    candidate_id: str
    run_id: str
    draft: CandidateDraft
    status: str


@dataclass(frozen=True)
class ExtractionSource:
    message: StoredMessage
    state_version: int


@dataclass(frozen=True)
class CandidateRun:
    run_id: str
    processor_version: str
    source_ids: tuple[str, ...]
    state: str
    attempt: int
    source_watermark: str
    candidate_count: int
    error_code: str | None
    lease_until: float | None
    completed_watermark: str | None


@dataclass(frozen=True)
class CandidateLease:
    status: CandidateRun
    token: str | None = field(repr=False)
    sources: tuple[ExtractionSource, ...]


class CandidateWorkflow:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS candidate_runs_v1 (
            id TEXT PRIMARY KEY, request_key BLOB UNIQUE NOT NULL, owner TEXT NOT NULL,
            processor TEXT NOT NULL, sources TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL,
            token_hash BLOB NOT NULL, lease_until REAL, watermark TEXT NOT NULL, result_hash BLOB, error_code TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS fact_candidates_v1 (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES candidate_runs_v1(id), draft TEXT NOT NULL)""")

    def _hash(self, value: str) -> bytes:
        return hmac.digest(self.runtime._fingerprint_key, value.encode("utf-8"), hashlib.sha256)

    def _sources(self, db: sqlite3.Connection, ids: tuple[str, ...]) -> tuple[ExtractionSource, ...]:
        sources: list[ExtractionSource] = []
        for message_id in ids:
            state = self.runtime._state(db, message_id)
            if state.status != "active":
                raise MemoryFailure("stale_source")
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
            stored = self.runtime._stored(row)
            if stored.event.completion != "complete" or stored.event.role not in {"user", "assistant", "tool"}:
                raise MemoryFailure("unsupported_extraction_source")
            sources.append(ExtractionSource(stored, state.version))
        return tuple(sources)

    @staticmethod
    def _versions(sources: tuple[ExtractionSource, ...]) -> str:
        return _json([[source.message.receipt.message_id, source.state_version,
                       source.message.receipt.representation, source.message.receipt.content_version] for source in sources])

    @staticmethod
    def _row(db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        _identifier(run_id)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_runs_v1'").fetchone():
            raise MemoryFailure("candidate_run_not_found")
        row = db.execute("SELECT * FROM candidate_runs_v1 WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise MemoryFailure("candidate_run_not_found")
        assert isinstance(row, sqlite3.Row)
        return row

    def _current(self, db: sqlite3.Connection, row: sqlite3.Row) -> bool:
        try:
            ids = tuple(item[0] for item in json.loads(row["sources"]))
            return self._versions(self._sources(db, ids)) == str(row["sources"])
        except MemoryFailure as error:
            if error.code not in {"stale_source", "message_not_found", "unsupported_extraction_source"}:
                raise
            return False

    def _status(self, db: sqlite3.Connection, row: sqlite3.Row) -> CandidateRun:
        state = "expired" if row["state"] == "running" and float(row["lease_until"]) <= time.time() else row["state"]
        if not self._current(db, row):
            state = "stale"
        count = int(db.execute("SELECT count(*) FROM fact_candidates_v1 WHERE run_id=?", (row["id"],)).fetchone()[0])
        return CandidateRun(row["id"], row["processor"], tuple(item[0] for item in json.loads(row["sources"])),
            state, int(row["attempt"]), row["watermark"], count if state == "succeeded" else 0,
            "stale_source" if state == "stale" else row["error_code"], row["lease_until"],
            row["watermark"] if state == "succeeded" else None)

    def _lease(self, row: sqlite3.Row, owner: str, token: str) -> None:
        if (not isinstance(token, str) or not 1 <= len(token) <= 128 or not token.isascii()
                or row["owner"] != owner or not hmac.compare_digest(bytes(row["token_hash"]), self._hash(token))):
            raise MemoryFailure("invalid_candidate_lease")
        if row["state"] not in {"succeeded", "failed"} and float(row["lease_until"]) <= time.time():
            raise MemoryFailure("candidate_lease_expired")

    def begin(self, access: Access, source_ids: tuple[str, ...], *, processor_version: str,
              lease_seconds: int = 300) -> CandidateLease:
        identity = self.runtime._require_scope(access, "maintain", "candidate_begin")
        _identifier(processor_version)
        if (not isinstance(source_ids, tuple) or not 1 <= len(source_ids) <= 16
                or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600):
            raise MemoryFailure("invalid_candidate_run")
        for message_id in source_ids:
            _identifier(message_id)
        ids = tuple(sorted(set(source_ids)))
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            sources = self._sources(db, ids)
            versions = self._versions(sources)
            request_key = self._hash(_json([processor_version, json.loads(versions)]))
            existing = db.execute("SELECT * FROM candidate_runs_v1 WHERE request_key=?", (request_key,)).fetchone()
            if existing is not None:
                if existing["state"] == "succeeded":
                    return CandidateLease(self._status(db, existing), None, ())
                if existing["state"] == "running" and float(existing["lease_until"]) > time.time():
                    raise MemoryFailure("candidate_run_busy")
            token = secrets.token_urlsafe(32)
            run_id = str(uuid4()) if existing is None else str(existing["id"])
            watermark = max(source.message.receipt.received_at for source in sources)
            if existing is None:
                db.execute("INSERT INTO candidate_runs_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, request_key, identity.client_id, processor_version, versions, "running", 1,
                     self._hash(token), time.time() + lease_seconds, watermark, None, None))
            else:
                db.execute("""UPDATE candidate_runs_v1 SET owner=?,state='running',attempt=attempt+1,
                    token_hash=?,lease_until=?,result_hash=NULL,error_code=NULL WHERE id=?""",
                    (identity.client_id, self._hash(token), time.time() + lease_seconds, run_id))
            status = self._status(db, self._row(db, run_id))
            db.execute("COMMIT")
        return CandidateLease(status, token, sources)

    def complete(self, access: Access, run_id: str, token: str, drafts: tuple[CandidateDraft, ...]) -> CandidateRun:
        identity = self.runtime._require_scope(access, "maintain", "candidate_complete")
        if not isinstance(token, str) or not isinstance(drafts, tuple) or len(drafts) > 32:
            raise MemoryFailure("invalid_candidate_batch")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, run_id)
            self._lease(row, identity.client_id, token)
            if row["state"] not in {"running", "succeeded"}:
                raise MemoryFailure("invalid_candidate_lease")
            source_ids = tuple(item[0] for item in json.loads(row["sources"]))
            sources = self._sources(db, source_ids)
            if self._versions(sources) != row["sources"]:
                raise MemoryFailure("stale_source")
            encoded: set[str] = set()
            for draft in drafts:
                self._validate(draft, sources)
                encoded.add(_json(asdict(draft)))
            result_hash = self._hash(_json(sorted(encoded)))
            if row["state"] == "succeeded":
                if not hmac.compare_digest(bytes(row["result_hash"]), result_hash):
                    raise MemoryFailure("candidate_result_conflict")
                return self._status(db, row)
            for payload in sorted(encoded):
                candidate_id = self._hash(_json([run_id, payload])).hex()
                db.execute("INSERT INTO fact_candidates_v1 VALUES (?,?,?)", (candidate_id, run_id, payload))
            self._lease(row, identity.client_id, token)
            db.execute("UPDATE candidate_runs_v1 SET state='succeeded',result_hash=?,lease_until=NULL WHERE id=?", (result_hash, run_id))
            result = self._status(db, self._row(db, run_id))
            db.execute("COMMIT")
        return result

    def fail(self, access: Access, run_id: str, token: str, *, error_code: str = "extractor_failed") -> CandidateRun:
        identity = self.runtime._require_scope(access, "maintain", "candidate_fail")
        if not isinstance(error_code, str) or error_code not in {"extractor_failed", "canceled", "timed_out", "invalid_output", "input_not_admitted", "dependency_unavailable"}:
            raise MemoryFailure("invalid_candidate_failure")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, run_id)
            self._lease(row, identity.client_id, token)
            if row["state"] == "succeeded":
                raise MemoryFailure("candidate_result_conflict")
            db.execute("UPDATE candidate_runs_v1 SET state='failed',error_code=?,lease_until=NULL WHERE id=?", (error_code, run_id))
            result = self._status(db, self._row(db, run_id))
            db.execute("COMMIT")
        return result

    @staticmethod
    def _validate(draft: CandidateDraft, sources: tuple[ExtractionSource, ...]) -> None:
        if (not isinstance(draft, CandidateDraft) or not isinstance(draft.text, str) or not draft.text.strip()
                or len(draft.text) > 4096 or type(draft.disputed) is not bool
                or not isinstance(draft.provenance, str) or draft.provenance not in {"user_statement", "assistant_generated", "quoted", "roleplay"}
                or not isinstance(draft.sources, tuple) or not 1 <= len(draft.sources) <= 16):
            raise MemoryFailure("invalid_candidate")
        try:
            draft.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_candidate") from None
        if credential_signal(draft.text):
            raise MemoryFailure("protected_candidate")
        available = {source.message.receipt.message_id: source for source in sources}
        for span in draft.sources:
            if not isinstance(span, EvidenceSpan) or not isinstance(span.message_id, str) or span.message_id not in available:
                raise MemoryFailure("invalid_candidate_source")
            source = available[span.message_id]
            receipt, event = source.message.receipt, source.message.event
            if not receipt.original_saved:
                raise MemoryFailure("protected_candidate")
            if (type(span.state_version) is not int or span.state_version != source.state_version
                    or type(span.content_version) is not int or span.content_version != receipt.content_version
                    or span.representation != receipt.representation or type(span.start) is not int or type(span.end) is not int
                    or not 0 <= span.start < span.end <= len(event.text) or span.end - span.start > 16384
                    or not isinstance(span.expected_text, str) or event.text[span.start:span.end] != span.expected_text):
                raise MemoryFailure("candidate_span_mismatch")
            if ((draft.provenance == "user_statement" and event.role != "user")
                    or (draft.provenance == "assistant_generated" and event.role != "assistant")):
                raise MemoryFailure("candidate_provenance_mismatch")

    def status(self, access: Access, run_id: str) -> CandidateRun:
        self.runtime._require_scope(access, "maintain", "candidate_status")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            return self._status(db, self._row(db, run_id))

    def candidates(self, access: Access, run_id: str) -> tuple[FactCandidate, ...]:
        self.runtime._require_scope(access, "maintain", "candidate_list")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            row = self._row(db, run_id)
            if self._status(db, row).state != "succeeded":
                return ()
            result: list[FactCandidate] = []
            for candidate in db.execute("SELECT id,draft FROM fact_candidates_v1 WHERE run_id=? ORDER BY id", (run_id,)):
                value = json.loads(candidate["draft"])
                value["sources"] = tuple(EvidenceSpan(**span) for span in value["sources"])
                draft = CandidateDraft(**value)
                result.append(FactCandidate(candidate["id"], run_id, draft, "disputed" if draft.disputed else "candidate"))
            return tuple(result)
