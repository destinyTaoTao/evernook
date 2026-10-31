"""Versioned core persona blocks. Only user-confirmed explicit operations write; consolidation never mutates persona."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3

from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal


PERSONA_BLOCKS = ("identity", "tone", "boundary", "address", "state")
_PERSONA_TEXT_LIMIT = 16384


@dataclass(frozen=True)
class PersonaSet:
    request_id: str
    block: str
    text: str = field(repr=False)
    evidence_id: str
    reason: str
    expected_revision: int


@dataclass(frozen=True)
class PersonaRollback:
    request_id: str
    target_seq: int
    expected_seq: int
    evidence_id: str
    reason: str


@dataclass(frozen=True)
class PersonaBlockRecord:
    block: str
    revision: int
    seq: int
    action: str
    text: str = field(repr=False)
    evidence_id: str
    evidence_version: int
    reason: str
    request_id: str
    restored_from: int | None
    recorded_at: str


@dataclass(frozen=True)
class PersonaRollbackResult:
    request_id: str
    target_seq: int
    head_seq: int
    restored: tuple[PersonaBlockRecord, ...]
    unchanged: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True)
class PersonaView:
    head_seq: int
    as_of_seq: int
    blocks: dict[str, PersonaBlockRecord | None]


@dataclass(frozen=True)
class PersonaHistoryPage:
    records: tuple[PersonaBlockRecord, ...]
    next_offset: int | None


def _encode_response(value: PersonaBlockRecord | PersonaRollbackResult) -> str:
    return _json({"kind": "block" if isinstance(value, PersonaBlockRecord) else "rollback", "value": asdict(value)})


def _decode_response(raw: str) -> PersonaBlockRecord | PersonaRollbackResult:
    value = json.loads(raw)
    if value["kind"] == "block":
        return PersonaBlockRecord(**value["value"])
    result = value["value"]
    result["restored"] = tuple(PersonaBlockRecord(**item) for item in result["restored"])
    result["unchanged"] = tuple(result["unchanged"])
    return PersonaRollbackResult(**result)


class PersonaRegistry:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS persona_requests_v1 (
            request_id TEXT PRIMARY KEY, fingerprint BLOB NOT NULL, response TEXT NOT NULL,
            recorded_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS persona_versions_v1 (
            block TEXT NOT NULL, revision INTEGER NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL,
            evidence_id TEXT NOT NULL, evidence_version INTEGER NOT NULL, reason TEXT NOT NULL,
            text TEXT NOT NULL, restored_from INTEGER, recorded_at TEXT NOT NULL,
            seq INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(block,revision))""")

    @staticmethod
    def _record(row: sqlite3.Row) -> PersonaBlockRecord:
        return PersonaBlockRecord(row["block"], int(row["revision"]), int(row["seq"]), row["action"], row["text"],
            row["evidence_id"], int(row["evidence_version"]), row["reason"], row["request_id"],
            None if row["restored_from"] is None else int(row["restored_from"]), row["recorded_at"])

    @classmethod
    def _validate_text(cls, text: str) -> None:
        if not isinstance(text, str):
            raise MemoryFailure("invalid_persona")
        try:
            encoded = text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if len(encoded) > _PERSONA_TEXT_LIMIT:
            raise MemoryFailure("persona_too_large")
        if credential_signal(text):
            raise MemoryFailure("protected_persona")

    @staticmethod
    def _validate_frame(request_id: str, evidence_id: str, reason: str) -> None:
        _identifier(request_id)
        _identifier(evidence_id)
        _identifier(reason)
        if not reason.strip():
            raise MemoryFailure("invalid_persona")

    def _evidence(self, db: sqlite3.Connection, evidence_id: str) -> int:
        state = self.runtime._state(db, evidence_id)
        row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (evidence_id,)).fetchone()
        message = self.runtime._stored(row)
        if (state.status != "active" or message.event.role != "user" or message.event.completion != "complete"
                or not message.receipt.original_saved or not message.event.text.strip()):
            raise MemoryFailure("persona_user_confirmation_required")
        return state.version

    def set(self, access: Access, request: PersonaSet) -> PersonaBlockRecord:
        """Trusted local boundary: the caller must establish the user's actual confirmation."""
        self.runtime._authorize(access)
        if not isinstance(request, PersonaSet) or type(request.expected_revision) is not int or request.expected_revision < 0:
            raise MemoryFailure("invalid_persona")
        if request.block not in PERSONA_BLOCKS:
            raise MemoryFailure("invalid_persona")
        self._validate_text(request.text)
        self._validate_frame(request.request_id, request.evidence_id, request.reason)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,response FROM persona_requests_v1 WHERE request_id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("persona_request_conflict")
                db.execute("COMMIT")
                value = _decode_response(existing["response"])
                assert isinstance(value, PersonaBlockRecord)
                return value
            head = self._latest(db, request.block)
            if (0 if head is None else head.revision) != request.expected_revision:
                raise MemoryFailure("persona_revision_conflict")
            evidence_version = self._evidence(db, request.evidence_id)
            now = datetime.now(timezone.utc).isoformat()
            record = PersonaBlockRecord(request.block, 1 if head is None else head.revision + 1, 0, "set",
                request.text, request.evidence_id, evidence_version, request.reason, request.request_id, None, now)
            db.execute("""INSERT INTO persona_versions_v1
                (block,revision,request_id,action,evidence_id,evidence_version,reason,text,restored_from,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,NULL,?)""",
                (record.block, record.revision, record.request_id, record.action, record.evidence_id,
                 record.evidence_version, record.reason, record.text, record.recorded_at))
            record = replace_seq(record, db)
            db.execute("INSERT INTO persona_requests_v1 VALUES (?,?,?,?)",
                (request.request_id, fingerprint, _encode_response(record), record.recorded_at))
            db.execute("COMMIT")
        return record

    def rollback(self, access: Access, request: PersonaRollback) -> PersonaRollbackResult:
        """Restore every block to its content as of target_seq; the rollback itself is a new traceable change."""
        self.runtime._authorize(access)
        if (not isinstance(request, PersonaRollback) or type(request.target_seq) is not int
                or type(request.expected_seq) is not int or request.target_seq < 1 or request.expected_seq < 0):
            raise MemoryFailure("invalid_persona")
        self._validate_frame(request.request_id, request.evidence_id, request.reason)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,response FROM persona_requests_v1 WHERE request_id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("persona_request_conflict")
                db.execute("COMMIT")
                value = _decode_response(existing["response"])
                assert isinstance(value, PersonaRollbackResult)
                return value
            head_seq = self._head_seq(db)
            if request.expected_seq != head_seq:
                raise MemoryFailure("persona_seq_conflict")
            if request.target_seq >= head_seq:
                raise MemoryFailure("invalid_persona_target")
            evidence_version = self._evidence(db, request.evidence_id)
            now = datetime.now(timezone.utc).isoformat()
            restored: list[PersonaBlockRecord] = []
            unchanged: list[str] = []
            for block in PERSONA_BLOCKS:
                target = self._latest(db, block, as_of_seq=request.target_seq)
                head = self._latest(db, block)
                if ("" if target is None else target.text) == ("" if head is None else head.text):
                    unchanged.append(block)
                    continue
                record = PersonaBlockRecord(block, 1 if head is None else head.revision + 1, 0, "rollback",
                    "" if target is None else target.text, request.evidence_id, evidence_version,
                    request.reason, request.request_id, request.target_seq, now)
                db.execute("""INSERT INTO persona_versions_v1
                    (block,revision,request_id,action,evidence_id,evidence_version,reason,text,restored_from,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (record.block, record.revision, record.request_id, record.action, record.evidence_id,
                     record.evidence_version, record.reason, record.text, record.restored_from, record.recorded_at))
                restored.append(replace_seq(record, db))
            result = PersonaRollbackResult(request.request_id, request.target_seq, self._head_seq(db),
                tuple(restored), tuple(unchanged), now)
            db.execute("INSERT INTO persona_requests_v1 VALUES (?,?,?,?)",
                (request.request_id, fingerprint, _encode_response(result), now))
            db.execute("COMMIT")
        return result

    def view(self, access: Access, *, as_of_seq: int | None = None) -> PersonaView:
        self.runtime._authorize(access)
        if as_of_seq is not None and (type(as_of_seq) is not int or as_of_seq < 1):
            raise MemoryFailure("invalid_persona")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            head = self._head_seq(db)
            as_of = head if as_of_seq is None else as_of_seq
            return PersonaView(head, as_of,
                {block: self._latest(db, block, as_of_seq=None if as_of_seq is None else as_of_seq) for block in PERSONA_BLOCKS})

    def history(self, access: Access, block: str, *, limit: int = 100, offset: int = 0) -> PersonaHistoryPage:
        self.runtime._authorize(access)
        if block not in PERSONA_BLOCKS or type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_persona")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_versions_v1'").fetchone():
                return PersonaHistoryPage((), None)
            rows = db.execute("""SELECT * FROM persona_versions_v1 WHERE block=? ORDER BY revision LIMIT ? OFFSET ?""",
                (block, limit + 1, offset)).fetchall()
            records = tuple(self._record(row) for row in rows[:limit])
            return PersonaHistoryPage(records, offset + limit if len(rows) > limit else None)

    @staticmethod
    def _head_seq(db: sqlite3.Connection) -> int:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_versions_v1'").fetchone():
            return 0
        row = db.execute("SELECT COALESCE(MAX(seq),0) FROM persona_versions_v1").fetchone()
        return int(row[0])

    @classmethod
    def _latest(cls, db: sqlite3.Connection, block: str, *, as_of_seq: int | None = None) -> PersonaBlockRecord | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='persona_versions_v1'").fetchone():
            return None
        if as_of_seq is None:
            row = db.execute("SELECT * FROM persona_versions_v1 WHERE block=? ORDER BY revision DESC LIMIT 1", (block,)).fetchone()
        else:
            row = db.execute("SELECT * FROM persona_versions_v1 WHERE block=? AND seq<=? ORDER BY revision DESC LIMIT 1",
                (block, as_of_seq)).fetchone()
        return None if row is None else cls._record(row)


def replace_seq(record: PersonaBlockRecord, db: sqlite3.Connection) -> PersonaBlockRecord:
    """Read back the AUTOINCREMENT seq assigned to the row this record just inserted."""
    row = db.execute("SELECT seq FROM persona_versions_v1 WHERE block=? AND revision=?", (record.block, record.revision)).fetchone()
    assert row is not None
    return replace(record, seq=int(row["seq"]))
