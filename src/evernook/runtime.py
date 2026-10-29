"""Versioned local text ledger. Transport adapters call these same checked operations."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any
from uuid import uuid4

from .models import (
    Access, ArchiveView, Change, ChangeRecord, ContextReceipt, DerivedContext, Event,
    ImportantItem, ImportantMark, ImportantPage, MemoryFailure, MessageState, Page,
    Receipt, RuntimeStatus, Source, StoredMessage,
)
from .paths import external, provision_private_directory, verify_private_directory
from .safety import credential_signal


_RECALLABLE = "NOT EXISTS (SELECT 1 FROM visibility v WHERE v.message_id=messages.id AND v.status<>'active')"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise MemoryFailure("invalid_config")
        return value
    except (OSError, ValueError, UnicodeError):
        raise MemoryFailure("invalid_config") from None


def _time(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise MemoryFailure("invalid_time") from None


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(ord(char) < 32 for char in value):
        raise MemoryFailure("invalid_source")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MemoryFailure("invalid_encoding") from None
    if credential_signal(value):
        raise MemoryFailure("sensitive_metadata")


def initialize(root: Path, *, client_id: str, entry: str, allow_archive_review: bool = False) -> tuple[Path, Access]:
    _identifier(client_id)
    _identifier(entry)
    if type(allow_archive_review) is not bool:
        raise MemoryFailure("invalid_permissions")
    root = external(root)
    provision_private_directory(root)
    access = Access(client_id, secrets.token_urlsafe(32))
    config_path = root / "runtime.json"
    config_path.write_text(_json({"schema_version": 1, "data_dir": str(root), "busy_timeout_ms": 5000}), encoding="utf-8")
    (root / "client.json").write_text(_json({"schema_version": 2, "client_id": client_id, "entry": entry, "token": access.token, "archive_review": allow_archive_review}), encoding="utf-8")
    (root / "idempotency.key").write_bytes(secrets.token_bytes(32))
    MemoryRuntime(config_path)
    return config_path, access


class MemoryRuntime:
    def __init__(self, config_path: Path):
        config_path = external(config_path)
        config = _load(config_path)
        if set(config) != {"schema_version", "data_dir", "busy_timeout_ms"} or type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise MemoryFailure("unsupported_config")
        if not isinstance(config["data_dir"], str) or type(config["busy_timeout_ms"]) is not int or not 0 <= config["busy_timeout_ms"] <= 30000:
            raise MemoryFailure("invalid_config")
        self.root = external(Path(config["data_dir"]))
        if config_path.parent != self.root:
            raise MemoryFailure("invalid_config_location")
        self._timeout = config["busy_timeout_ms"] / 1000
        for name in ["client.json", "idempotency.key", "memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm"]:
            if external(self.root / name).parent != self.root:
                raise MemoryFailure("unsafe_runtime_path")
        verify_private_directory(self.root)
        credentials = _load(self.root / "client.json")
        if type(credentials.get("schema_version")) is not int or credentials["schema_version"] not in (1, 2):
            raise MemoryFailure("invalid_credentials")
        fields = {"schema_version", "client_id", "entry", "token"}
        if credentials["schema_version"] == 2:
            fields.add("archive_review")
        if set(credentials) != fields or type(credentials.get("archive_review", False)) is not bool:
            raise MemoryFailure("invalid_credentials")
        self._archive_review: bool = credentials.get("archive_review", False)
        if any(not isinstance(credentials[key], str) for key in ["client_id", "entry", "token"]) or len(credentials["token"]) < 40:
            raise MemoryFailure("invalid_credentials")
        self._client_id: str = credentials["client_id"]
        self._entry: str = credentials["entry"]
        self._token: str = credentials["token"]
        _identifier(self._client_id)
        _identifier(self._entry)
        try:
            self._fingerprint_key = (self.root / "idempotency.key").read_bytes()
        except OSError:
            raise MemoryFailure("idempotency_material_unavailable") from None
        if len(self._fingerprint_key) != 32:
            raise MemoryFailure("idempotency_material_unavailable")
        with self._connection() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, 2, 3):
                raise MemoryFailure("unsupported_storage_version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS messages (
                  id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, fingerprint BLOB NOT NULL,
                  event TEXT NOT NULL, receipt TEXT NOT NULL, occurred REAL, received TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_time ON messages(occurred,id);
                CREATE TABLE IF NOT EXISTS jobs (message_id TEXT PRIMARY KEY REFERENCES messages(id), state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS important (
                  id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
                  identity TEXT UNIQUE NOT NULL, item TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes (
                  request_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES messages(id),
                  evidence_id TEXT NOT NULL REFERENCES messages(id), fingerprint BLOB NOT NULL,
                  request TEXT NOT NULL, receipt TEXT NOT NULL, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS visibility (
                  message_id TEXT PRIMARY KEY REFERENCES messages(id), version INTEGER NOT NULL, status TEXT NOT NULL,
                  replacement_id TEXT REFERENCES messages(id), change_id TEXT NOT NULL REFERENCES changes(request_id));
                CREATE TABLE IF NOT EXISTS source_context (
                  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES messages(id), source_version INTEGER NOT NULL,
                  fingerprint BLOB NOT NULL, context TEXT NOT NULL, receipt TEXT NOT NULL);
                PRAGMA user_version=3;
                COMMIT;""")

    def _authorize(self, access: Access) -> None:
        if not isinstance(access, Access):
            raise MemoryFailure("unauthorized")
        if type(access.contract_version) is not int or access.contract_version != 1:
            raise MemoryFailure("incompatible_contract")
        if access.client_id != self._client_id or not isinstance(access.token, str):
            raise MemoryFailure("unauthorized")
        try:
            matches = hmac.compare_digest(access.token.encode(), self._token.encode())
        except UnicodeError:
            matches = False
        if not matches:
            raise MemoryFailure("unauthorized")

    def handshake(self, access: Access) -> dict[str, object]:
        self._authorize(access)
        return {"contract_version": 1, "ordinary_text": True, "important": True, "changes": True, "storage_version": 3,
                "archive_review": self._archive_review,
                "vault": False, "backup": "deferred", "sqlite_version": sqlite3.sqlite_version}

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.root / "memory.sqlite3", timeout=self._timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 255
            raise MemoryFailure({sqlite3.SQLITE_BUSY: "storage_busy", sqlite3.SQLITE_LOCKED: "storage_busy", sqlite3.SQLITE_FULL: "storage_full"}.get(code, "storage_unavailable")) from None
        finally:
            if connection is not None:
                connection.close()

    def submit(self, access: Access, event: Event) -> Receipt:
        self._authorize(access)
        self._validate_event(event)
        if event.source.entry != self._entry:
            raise MemoryFailure("unauthorized_source")
        source = _json(asdict(event.source))
        fingerprint = hmac.digest(self._fingerprint_key, _json(asdict(event)).encode(), hashlib.sha256)
        retained = replace(event, text="[PROTECTED:ORIGINAL_NOT_SAVED]") if event.sensitive or event.protected_source or credential_signal(event.text) else event
        projected = retained is not event
        receipt = Receipt(str(uuid4()), datetime.now(timezone.utc).isoformat(), "projection_only" if projected else "committed", "projection" if projected else "original", not projected)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT fingerprint, receipt FROM messages WHERE source=?", (source,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("source_conflict")
                return Receipt(**json.loads(existing["receipt"]))
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (receipt.message_id, source, fingerprint, _json(asdict(retained)), _json(asdict(receipt)), _time(event.occurred_at) if event.occurred_at is not None else None, receipt.received_at))
            db.execute("INSERT INTO jobs VALUES (?,?)", (receipt.message_id, "pending"))
            db.execute("COMMIT")
        return receipt

    def _validate_event(self, event: Event) -> None:
        if not isinstance(event, Event) or not isinstance(event.source, Source):
            raise MemoryFailure("invalid_event")
        for value in asdict(event.source).values():
            _identifier(value)
        if not isinstance(event.role, str) or not isinstance(event.completion, str) or event.role not in {"user", "assistant", "tool", "system", "developer"} or event.completion not in {"partial", "complete", "canceled", "failed"}:
            raise MemoryFailure("invalid_event")
        if type(event.sequence) is not int or event.sequence < 0 or type(event.sensitive) is not bool or type(event.protected_source) is not bool or not isinstance(event.text, str):
            raise MemoryFailure("invalid_event")
        if event.occurred_at is not None:
            _time(event.occurred_at)
        try:
            encoded = event.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if len(encoded) > 1048576:
            raise MemoryFailure("message_too_large")

    @staticmethod
    def _stored(row: sqlite3.Row) -> StoredMessage:
        value = json.loads(row["event"])
        value["source"] = Source(**value["source"])
        return StoredMessage(Event(**value), Receipt(**json.loads(row["receipt"])))

    def read(self, access: Access, message_id: str) -> StoredMessage | None:
        self._authorize(access)
        _identifier(message_id)
        with self._connection() as db:
            row = db.execute(f"SELECT event,receipt FROM messages WHERE id=? AND {_RECALLABLE}", (message_id,)).fetchone()
            return self._stored(row) if row is not None else None

    def read_source(self, access: Access, source: Source) -> StoredMessage | None:
        self._authorize(access)
        if not isinstance(source, Source):
            raise MemoryFailure("invalid_source")
        for value in asdict(source).values():
            _identifier(value)
        with self._connection() as db:
            row = db.execute(f"SELECT event,receipt FROM messages WHERE source=? AND {_RECALLABLE}", (_json(asdict(source)),)).fetchone()
            return self._stored(row) if row is not None else None

    def list_messages(self, access: Access, *, since: str | None = None, until: str | None = None, limit: int = 100, offset: int = 0) -> Page:
        self._authorize(access)
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_page")
        start, end = _time(since) if since is not None else None, _time(until) if until is not None else None
        if start is not None and end is not None and start >= end:
            raise MemoryFailure("invalid_time_range")
        with self._connection() as db:
            rows = db.execute(f"""SELECT event,receipt FROM messages
                WHERE (? IS NULL OR occurred>=?) AND (? IS NULL OR occurred<?) AND {_RECALLABLE}
                ORDER BY occurred, json_extract(event,'$.source.entry'), json_extract(event,'$.source.session'),
                         json_extract(event,'$.source.branch'), json_extract(event,'$.sequence'), id
                LIMIT ? OFFSET ?""", (start, start, end, end, limit + 1, offset)).fetchall()
            return Page(tuple(self._stored(row) for row in rows[:limit]), offset + limit if len(rows) > limit else None)

    def status(self, access: Access) -> RuntimeStatus:
        self._authorize(access)
        with self._connection() as db:
            counts = db.execute("SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM jobs WHERE state='pending'), (SELECT count(*) FROM important)").fetchone()
            return RuntimeStatus(int(counts[0]), int(counts[1]), important_items=int(counts[2]))

    def mark_important(self, access: Access, mark: ImportantMark) -> ImportantItem:
        self._authorize(access)
        if not isinstance(mark, ImportantMark):
            raise MemoryFailure("invalid_mark")
        _identifier(mark.message_id)
        _identifier(mark.scope)
        if not isinstance(mark.representation, str) or mark.representation not in {"original", "projection"} or type(mark.content_version) is not int or not isinstance(mark.expected_text, str):
            raise MemoryFailure("invalid_mark")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (mark.message_id,)).fetchone()
            if row is None:
                raise MemoryFailure("message_not_found")
            if self._state(db, mark.message_id).status != "active":
                raise MemoryFailure("not_recallable")
            message = self._stored(row)
            if mark.representation != message.receipt.representation:
                raise MemoryFailure("representation_mismatch")
            if mark.content_version != message.receipt.content_version:
                raise MemoryFailure("version_mismatch")
            text = message.event.text
            start, end = (0, len(text)) if mark.start is None and mark.end is None else (mark.start, mark.end)
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                raise MemoryFailure("invalid_span")
            if text[start:end] != mark.expected_text:
                raise MemoryFailure("span_mismatch")
            identity = _json([mark.message_id, mark.representation, mark.content_version, mark.scope, start, end])
            existing = db.execute("SELECT item FROM important WHERE identity=?", (identity,)).fetchone()
            if existing is not None:
                return ImportantItem(**json.loads(existing["item"]))
            item = ImportantItem(str(uuid4()), mark.message_id, mark.representation, mark.content_version,
                                 mark.scope, start, end, datetime.now(timezone.utc).isoformat())
            db.execute("INSERT INTO important VALUES (?,?,?,?)", (item.important_id, item.message_id, identity, _json(asdict(item))))
            db.execute("COMMIT")
        return item

    def read_important(self, access: Access, important_id: str, *, cursor: int | None = None,
                       limit: int = 2048, content_version: int = 1) -> ImportantPage:
        self._authorize(access)
        _identifier(important_id)
        if type(limit) is not int or not 1 <= limit <= 4096 or (cursor is not None and type(cursor) is not int):
            raise MemoryFailure("invalid_page")
        with self._connection() as db:
            row = db.execute("""SELECT important.item, messages.event, COALESCE(v.status,'active') AS status
                FROM important JOIN messages ON messages.id=important.message_id
                LEFT JOIN visibility v ON v.message_id=messages.id WHERE important.id=?""", (important_id,)).fetchone()
            if row is None:
                raise MemoryFailure("important_not_found")
            if row["status"] != "active":
                raise MemoryFailure("not_recallable")
            item = ImportantItem(**json.loads(row["item"]))
            if type(content_version) is not int or content_version != item.content_version:
                raise MemoryFailure("version_mismatch")
            text: str = json.loads(row["event"])["text"]
            start = item.start if cursor is None else cursor
            if not item.start <= start < item.end:
                raise MemoryFailure("invalid_cursor")
            end = min(start + limit, item.end)
            return ImportantPage(item, text[start:end], start, end, item.end - end, end if end < item.end else None)

    @staticmethod
    def _state(db: sqlite3.Connection, message_id: str) -> MessageState:
        row = db.execute("""SELECT messages.id, COALESCE(v.version,1) AS version,
            COALESCE(v.status,'active') AS status, v.replacement_id, v.change_id
            FROM messages LEFT JOIN visibility v ON v.message_id=messages.id WHERE messages.id=?""", (message_id,)).fetchone()
        if row is None:
            raise MemoryFailure("message_not_found")
        return MessageState(str(row["id"]), int(row["version"]), str(row["status"]), row["replacement_id"], row["change_id"])

    def state(self, access: Access, message_id: str) -> MessageState:
        self._authorize(access)
        _identifier(message_id)
        with self._connection() as db:
            return self._state(db, message_id)

    def change(self, access: Access, request: Change) -> MessageState:
        self._authorize(access)
        if not isinstance(request, Change) or type(request.expected_version) is not int or request.expected_version < 1:
            raise MemoryFailure("invalid_change")
        for value in [request.request_id, request.target_id, request.evidence_id, request.reason]:
            _identifier(value)
        if not request.reason.strip() or not isinstance(request.action, str) or request.action not in {"correct", "revoke", "suppress"}:
            raise MemoryFailure("invalid_change")
        fingerprint = hmac.digest(self._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT fingerprint,receipt FROM changes WHERE request_id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("change_conflict")
                return MessageState(**json.loads(existing["receipt"]))
            current = self._state(db, request.target_id)
            if current.version != request.expected_version:
                raise MemoryFailure("version_conflict")
            evidence_row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (request.evidence_id,)).fetchone()
            if evidence_row is None or request.evidence_id == request.target_id:
                raise MemoryFailure("insufficient_evidence")
            evidence = self._stored(evidence_row)
            if (evidence.event.role != "user" or evidence.event.completion != "complete"
                    or not evidence.event.text.strip() or not evidence.receipt.original_saved
                    or self._state(db, request.evidence_id).status != "active"):
                raise MemoryFailure("insufficient_evidence")
            state = MessageState(current.message_id, current.version + 1,
                {"correct": "corrected", "revoke": "revoked", "suppress": "suppressed"}[request.action],
                request.evidence_id if request.action == "correct" else None, request.request_id)
            db.execute("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", (request.request_id, request.target_id,
                request.evidence_id, fingerprint, _json(asdict(request)), _json(asdict(state)), datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO visibility VALUES (?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET version=excluded.version, status=excluded.status, replacement_id=excluded.replacement_id, change_id=excluded.change_id",
                       (state.message_id, state.version, state.status, state.replacement_id, state.change_id))
            db.execute("COMMIT")
        return state

    def review_archive(self, access: Access, message_id: str) -> ArchiveView:
        self._authorize(access)
        if not self._archive_review:
            raise MemoryFailure("archive_forbidden")
        _identifier(message_id)
        with self._connection() as db:
            db.execute("BEGIN")
            state = self._state(db, message_id)
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
            records = db.execute("SELECT request,receipt,recorded_at FROM changes WHERE target_id=? ORDER BY json_extract(receipt,'$.version')", (message_id,)).fetchall()
            changes = tuple(ChangeRecord(Change(**json.loads(record["request"])), MessageState(**json.loads(record["receipt"])), record["recorded_at"]) for record in records)
            return ArchiveView(self._stored(row), state, changes)

    def publish_context(self, access: Access, context: DerivedContext) -> ContextReceipt:
        """Commit a caller-produced result only while its evidence version is still recallable."""
        self._authorize(access)
        if (not isinstance(context, DerivedContext) or type(context.source_version) is not int
                or context.source_version < 1 or not isinstance(context.text, str) or type(context.protected) is not bool):
            raise MemoryFailure("invalid_context")
        _identifier(context.result_id)
        _identifier(context.source_id)
        try:
            encoded = context.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if len(encoded) > 1048576:
            raise MemoryFailure("message_too_large")
        if context.protected or credential_signal(context.text):
            raise MemoryFailure("protected_context")
        fingerprint = hmac.digest(self._fingerprint_key, _json(asdict(context)).encode(), hashlib.sha256)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, context.source_id)
            if state.status != "active" or state.version != context.source_version:
                raise MemoryFailure("stale_source")
            source = self._stored(db.execute("SELECT event,receipt FROM messages WHERE id=?", (context.source_id,)).fetchone())
            if not source.receipt.original_saved and context.text != source.event.text:
                raise MemoryFailure("protected_context")
            existing = db.execute("SELECT fingerprint,receipt FROM source_context WHERE id=?", (context.result_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("context_conflict")
                return ContextReceipt(**json.loads(existing["receipt"]))
            receipt = ContextReceipt(context.result_id, context.source_id, context.source_version, datetime.now(timezone.utc).isoformat())
            db.execute("INSERT INTO source_context VALUES (?,?,?,?,?,?)", (context.result_id, context.source_id,
                context.source_version, fingerprint, _json(asdict(context)), _json(asdict(receipt))))
            db.execute("COMMIT")
        return receipt

    def read_context(self, access: Access, result_id: str) -> DerivedContext | None:
        self._authorize(access)
        _identifier(result_id)
        with self._connection() as db:
            row = db.execute("""SELECT c.context FROM source_context c LEFT JOIN visibility v ON v.message_id=c.source_id
                WHERE c.id=? AND COALESCE(v.status,'active')='active' AND c.source_version=COALESCE(v.version,1)""", (result_id,)).fetchone()
            return DerivedContext(**json.loads(row["context"])) if row is not None else None
