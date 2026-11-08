"""Versioned local text ledger. Transport adapters call these same checked operations."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from .models import (
    Access, ArchiveView, AttachmentInfo, AttachmentInput, AuditPage, AuditRecord, Capture, CaptureReceipt, Change, ChangeRecord, ContextReceipt, DerivedContext, Event,
    ImportantItem, ImportantMark, ImportantPage, MemoryFailure, MessageState, Page,
    Receipt, RuntimeStatus, Source, StoredMessage, SearchHit, SearchPage, LexicalStatus,
)
from .paths import external, provision_private_directory, verify_private_directory
from .safety import credential_signal
from .lexical import NORMALIZATION_IDENTITY, index_terms, matching_spans, normalize, query_expression

if TYPE_CHECKING:
    from .archive import ArchiveStatus
    from .durable_stream import StreamPage, StreamReceipt


_RECALLABLE = "NOT EXISTS (SELECT 1 FROM visibility v WHERE v.message_id=messages.id AND v.status<>'active')"
_LEXICAL_CURRENT = f"""d.normalization_version={NORMALIZATION_IDENTITY}
      AND d.content_version=json_extract(messages.receipt,'$.content_version')"""
_LEXICAL_VALID_ROWS = f"""FROM lexical_docs d
    JOIN messages ON messages.id=d.message_id
    JOIN (SELECT DISTINCT message_id FROM lexical) tokens ON tokens.message_id=d.message_id
    WHERE {_LEXICAL_CURRENT}"""
_LEXICAL_INDEXED = f"SELECT d.message_id {_LEXICAL_VALID_ROWS}"
_LEXICAL_ACCELERATION_INDEX = "evernook_message_cover_v1"
_LEXICAL_ACCELERATION_SQL = (f"CREATE INDEX {_LEXICAL_ACCELERATION_INDEX} ON "
    "messages(id,received,json_extract(receipt,'$.content_version'))")
_LEXICAL_ACCELERATED_ROWS = f"""FROM lexical_docs d
    JOIN messages INDEXED BY {_LEXICAL_ACCELERATION_INDEX} ON messages.id=d.message_id
    JOIN (SELECT message_id FROM lexical GROUP BY message_id) tokens ON tokens.message_id=d.message_id
    WHERE {_LEXICAL_CURRENT}"""
_LEXICAL_ROWID_CACHE_LIMIT = 100_000


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stream_marker_source(source: Source) -> Source:
    return replace(source, event="stream:" + hashlib.sha256(_json(asdict(source)).encode()).hexdigest())


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


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _audit_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS audit_events_v1 (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, client_id TEXT NOT NULL,
        entry TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS audit_at ON audit_events_v1(at)")
    db.execute("CREATE TABLE IF NOT EXISTS audit_settings_v1 (id INTEGER PRIMARY KEY CHECK(id=1), retention_days INTEGER NOT NULL)")


def _prune_audit(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT retention_days FROM audit_settings_v1 WHERE id=1").fetchone()
    retention = int(row["retention_days"]) if row is not None else AUDIT_RETENTION_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat()
    db.execute("DELETE FROM audit_events_v1 WHERE at<?", (cutoff,))


def _pairing_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS pairing_lockout_v1 (
        id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL, locked_until TEXT,
        failure_limit INTEGER NOT NULL)""")


def _audit_row(db: sqlite3.Connection, at: str, client_id: str, entry: str, action: str, target: str, outcome: str) -> None:
    _audit_schema(db)
    db.execute("INSERT INTO audit_events_v1 (at,client_id,entry,action,target,outcome) VALUES (?,?,?,?,?,?)",
               (at, client_id, entry, action, target, outcome))


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(ord(char) < 32 for char in value):
        raise MemoryFailure("invalid_source")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MemoryFailure("invalid_encoding") from None
    if credential_signal(value):
        raise MemoryFailure("sensitive_metadata")


# Caller scope model: a paired identity carries an explicit scope set. Legacy files (schema 1-3)
# predate scopes and keep the full set so trusted-local workflows behave exactly as before.
SCOPES = ("recall", "capture", "maintain", "accept", "admin")
FULL_SCOPES = SCOPES
HARNESS_DEFAULT_SCOPES = ("recall", "capture")
PAIRING_FAILURE_LIMIT = 5
PAIRING_LOCK_MINUTES = 15
AUDIT_RETENTION_DAYS = 365


def _scope_set(value: object) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MemoryFailure("invalid_credentials")
    if len(value) != len(set(value)) or any(item not in SCOPES for item in value):
        raise MemoryFailure("invalid_credentials")
    return frozenset(value)


@dataclass(frozen=True)
class _Identity:
    client_id: str
    entry: str
    token: str = field(repr=False)
    archive_review: bool = False
    scopes: frozenset[str] = frozenset(FULL_SCOPES)


def _identities(value: dict[str, Any]) -> dict[str, _Identity]:
    version = value.get("schema_version")
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise MemoryFailure("invalid_credentials")
    if version in (3, 4):
        if set(value) != {"schema_version", "clients"} or not isinstance(value["clients"], list) or not value["clients"]:
            raise MemoryFailure("invalid_credentials")
        rows = value["clients"]
    else:
        fields = {"schema_version", "client_id", "entry", "token"}
        if version == 2:
            fields.add("archive_review")
        if set(value) != fields:
            raise MemoryFailure("invalid_credentials")
        rows = [{"client_id": value["client_id"], "entry": value["entry"], "token": value["token"],
                 "archive_review": value.get("archive_review", False)}]
    result: dict[str, _Identity] = {}
    entries: set[str] = set()
    for row in rows:
        fields = {"client_id", "entry", "token", "archive_review"} | ({"scopes"} if version == 4 else set())
        if (not isinstance(row, dict) or set(row) != fields
                or any(not isinstance(row[key], str) for key in ("client_id", "entry", "token"))
                or type(row["archive_review"]) is not bool or len(row["token"]) < 40):
            raise MemoryFailure("invalid_credentials")
        scopes = _scope_set(row["scopes"]) if version == 4 else frozenset(FULL_SCOPES)
        _identifier(row["client_id"])
        _identifier(row["entry"])
        if row["client_id"] in result or row["entry"] in entries:
            raise MemoryFailure("invalid_credentials")
        result[row["client_id"]] = _Identity(row["client_id"], row["entry"], row["token"], row["archive_review"], scopes)
        entries.add(row["entry"])
    return result


def _access_record(value: dict[str, Any]) -> Access:
    if (set(value) != {"client_id", "token", "contract_version"} or not isinstance(value["client_id"], str)
            or not isinstance(value["token"], str) or type(value["contract_version"]) is not int):
        raise MemoryFailure("invalid_access")
    return Access(**value)


def _atomic_json(path: Path, value: object) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".pair-", delete=False) as output:
            temporary = Path(output.name)
            output.write(_json(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise MemoryFailure("client_provision_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pair_client(config_path: Path, *, client_id: str, entry: str, access_path: Path,
                scopes: tuple[str, ...] | None = None) -> Access:
    """Trusted local provisioning only. This operation is never exposed as an MCP tool."""
    _identifier(client_id)
    _identifier(entry)
    if scopes is not None:
        if not isinstance(scopes, tuple) or any(not isinstance(item, str) or item not in SCOPES for item in scopes):
            raise MemoryFailure("invalid_scopes")
    runtime = MemoryRuntime(config_path)
    access_path = external(access_path)
    if access_path.parent != runtime.root or not access_path.name.startswith("mcp-access") or access_path.suffix != ".json":
        raise MemoryFailure("invalid_access_location")
    verify_private_directory(runtime.root, extra_files=(access_path,))
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _pairing_schema(db)
        _audit_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        lock = db.execute("SELECT failures,locked_until,failure_limit FROM pairing_lockout_v1 WHERE id=1").fetchone()
        failures = int(lock["failures"]) if lock is not None else 0
        failure_limit = int(lock["failure_limit"]) if lock is not None else PAIRING_FAILURE_LIMIT
        locked_until = lock["locked_until"] if lock is not None else None
        if locked_until is not None and _time(locked_until) > time.time():
            _audit_row(db, now, client_id, entry, "pair", "client:" + client_id, "locked")
            db.execute("COMMIT")
            raise MemoryFailure("pairing_locked")
        identities = _identities(_load(runtime.root / "client.json"))
        identity = identities.get(client_id)
        if ((identity is not None and identity.entry != entry)
                or any(other.entry == entry and other.client_id != client_id for other in identities.values())):
            failures += 1
            locked = datetime.now(timezone.utc) + timedelta(minutes=PAIRING_LOCK_MINUTES) if failures >= failure_limit else None
            db.execute("""INSERT INTO pairing_lockout_v1 VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                failures=excluded.failures, locked_until=excluded.locked_until, failure_limit=excluded.failure_limit""",
                (failures, locked.isoformat() if locked is not None else None, failure_limit))
            _audit_row(db, now, client_id, entry, "pair", "client:" + client_id, "denied")
            db.execute("COMMIT")
            raise MemoryFailure("client_conflict")
        if identity is None:
            identity = _Identity(client_id, entry, secrets.token_urlsafe(32),
                                 scopes=frozenset(scopes) if scopes is not None else frozenset(FULL_SCOPES))
        elif scopes is not None:
            identity = replace(identity, scopes=frozenset(scopes))
        access = Access(client_id, identity.token)
        if access_path.exists():
            try:
                existing_access = _access_record(_load(access_path))
            except MemoryFailure:
                raise MemoryFailure("access_file_conflict") from None
            if existing_access != access:
                raise MemoryFailure("access_file_conflict")
        identities[client_id] = identity
        _atomic_json(runtime.root / "client.json", {"schema_version": 4, "clients": [
            {"client_id": row.client_id, "entry": row.entry, "token": row.token,
             "archive_review": row.archive_review, "scopes": sorted(row.scopes)} for row in identities.values()]})
        if not access_path.exists():
            _atomic_json(access_path, asdict(access))
        db.execute("""INSERT INTO pairing_lockout_v1 VALUES (1,0,NULL,?) ON CONFLICT(id) DO UPDATE SET
            failures=0, failure_limit=excluded.failure_limit""", (failure_limit,))
        _audit_row(db, now, client_id, entry, "pair", "client:" + client_id, "ok")
        db.execute("COMMIT")
    return access


def unlock_pairing(config_path: Path) -> dict[str, object]:
    """Trusted local reset of the pairing lockout; recorded in the audit log."""
    runtime = MemoryRuntime(config_path)
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _pairing_schema(db)
        db.execute("UPDATE pairing_lockout_v1 SET failures=0, locked_until=NULL WHERE id=1")
        _audit_row(db, datetime.now(timezone.utc).isoformat(), "local-operator", "", "pair_unlock", "lockout", "ok")
        db.execute("COMMIT")
    return {"unlocked": True}


def configure_lockout(config_path: Path, *, failure_limit: int) -> dict[str, object]:
    if type(failure_limit) is not int or not 1 <= failure_limit <= 100:
        raise MemoryFailure("invalid_lockout_config")
    runtime = MemoryRuntime(config_path)
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _pairing_schema(db)
        db.execute("""INSERT INTO pairing_lockout_v1 VALUES (1,0,NULL,?) ON CONFLICT(id) DO UPDATE SET
            failure_limit=excluded.failure_limit""", (failure_limit,))
        _audit_row(db, datetime.now(timezone.utc).isoformat(), "local-operator", "", "lockout_configure",
                   "lockout", "ok")
        db.execute("COMMIT")
    return {"failure_limit": failure_limit}


def configure_audit(config_path: Path, *, retention_days: int) -> dict[str, object]:
    if type(retention_days) is not int or not 1 <= retention_days <= 36500:
        raise MemoryFailure("invalid_audit_config")
    runtime = MemoryRuntime(config_path)
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _audit_schema(db)
        db.execute("""INSERT INTO audit_settings_v1 VALUES (1,?) ON CONFLICT(id) DO UPDATE SET
            retention_days=excluded.retention_days""", (retention_days,))
        _audit_row(db, datetime.now(timezone.utc).isoformat(), "local-operator", "", "audit_configure",
                   "audit_settings", "ok")
        db.execute("COMMIT")
    return {"retention_days": retention_days}


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".provision-", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise MemoryFailure("client_provision_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def initialize(root: Path, *, client_id: str, entry: str, allow_archive_review: bool = False) -> tuple[Path, Access]:
    _identifier(client_id)
    _identifier(entry)
    if type(allow_archive_review) is not bool:
        raise MemoryFailure("invalid_permissions")
    root = external(root)
    provision_private_directory(root)
    access = Access(client_id, secrets.token_urlsafe(32))
    config_path = root / "runtime.json"
    # Provisioning files are staged and fsynced, then atomically moved into place: a crash mid-write
    # can no longer leave a half-written file that wedges the directory permanently.
    _atomic_json(config_path, {"schema_version": 1, "data_dir": str(root), "busy_timeout_ms": 5000})
    _atomic_json(root / "client.json", {"schema_version": 2, "client_id": client_id, "entry": entry, "token": access.token, "archive_review": allow_archive_review})
    _atomic_bytes(root / "idempotency.key", secrets.token_bytes(32))
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
        _identities(_load(self.root / "client.json"))
        try:
            self._fingerprint_key = (self.root / "idempotency.key").read_bytes()
        except OSError:
            raise MemoryFailure("idempotency_material_unavailable") from None
        if len(self._fingerprint_key) != 32:
            raise MemoryFailure("idempotency_material_unavailable")
        ledger_id = hmac.digest(self._fingerprint_key, b"evernook-ledger-identity-v1", hashlib.sha256).hex()
        with self._connection() as db:
            # A complete current ledger needs only a read snapshot; migrations still take the writer lock.
            db.execute("BEGIN")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, 2, 3, 4, 5):
                raise MemoryFailure("unsupported_storage_version")
            required = {"messages", "message_time", "jobs", "important", "changes", "visibility",
                        "source_context", "captures", "attachments", "ledger_identity_v1",
                        "text_streams_v1", "text_stream_chunks_v1"}
            present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
            if version == 5 and required <= present and db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                stored_id = db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()
                if stored_id is not None:
                    if not hmac.compare_digest(str(stored_id[0]), ledger_id):
                        raise MemoryFailure("ledger_identity_mismatch")
                    return
            db.execute("ROLLBACK")
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
                CREATE TABLE IF NOT EXISTS captures (
                  message_id TEXT PRIMARY KEY REFERENCES messages(id), input_id TEXT REFERENCES messages(id), manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attachments (
                  message_id TEXT NOT NULL REFERENCES captures(message_id), attachment_id TEXT NOT NULL,
                  content BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(message_id,attachment_id));
                PRAGMA user_version=5;
                COMMIT;""")
            # A non-secret, key-bound ledger identity travels with ordinary snapshots; the
            # idempotency key itself remains outside archives and is never copied into them.
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS ledger_identity_v1 (id INTEGER PRIMARY KEY CHECK(id=1), ledger_id TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO ledger_identity_v1 VALUES (1,?)", (ledger_id,))
            stored_id = db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()
            if stored_id is None or not hmac.compare_digest(str(stored_id[0]), ledger_id):
                raise MemoryFailure("ledger_identity_mismatch")
            db.execute("COMMIT")
            from .durable_stream import install as install_streams
            install_streams(db)

    def _authorize(self, access: Access) -> _Identity:
        if not isinstance(access, Access):
            raise MemoryFailure("unauthorized")
        if type(access.contract_version) is not int or access.contract_version != 1:
            raise MemoryFailure("incompatible_contract")
        if not isinstance(access.client_id, str) or not isinstance(access.token, str):
            raise MemoryFailure("unauthorized")
        identity = _identities(_load(self.root / "client.json")).get(access.client_id)
        if identity is None:
            raise MemoryFailure("unauthorized")
        try:
            matches = hmac.compare_digest(access.token.encode(), identity.token.encode())
        except UnicodeError:
            matches = False
        if not matches:
            raise MemoryFailure("unauthorized")
        return identity

    def _require_scope(self, access: Access, scope: str, action: str, target: str = "") -> _Identity:
        identity = self._authorize(access)
        if scope not in identity.scopes:
            self._audit(identity, action, target, "denied")
            raise MemoryFailure("permission_denied")
        self._audit(identity, action, target, "ok")
        return identity

    def _audit(self, identity: _Identity, action: str, target: str, outcome: str) -> None:
        # Best-effort observability: audit writes must never block or fail the caller operation,
        # including fault-injection scenarios. Rows that are written remain append-only.
        try:
            with self._connection() as db:
                db.execute("PRAGMA busy_timeout=0")
                db.execute("BEGIN IMMEDIATE")
                _audit_schema(db)
                _prune_audit(db)
                db.execute("INSERT INTO audit_events_v1 (at,client_id,entry,action,target,outcome) VALUES (?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), identity.client_id, identity.entry, action, target, outcome))
                db.execute("COMMIT")
        except MemoryFailure:
            return

    def audit_query(self, access: Access, *, client_id: str | None = None, since: str | None = None,
                    until: str | None = None, limit: int = 100, offset: int = 0) -> AuditPage:
        self._require_scope(access, "maintain", "audit_query")
        if client_id is not None:
            _identifier(client_id)
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_page")
        start, end = _time(since) if since is not None else None, _time(until) if until is not None else None
        if start is not None and end is not None and start >= end:
            raise MemoryFailure("invalid_time_range")
        with self._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_events_v1'").fetchone():
                return AuditPage((), None)
            rows = db.execute("""SELECT seq,at,client_id,entry,action,target,outcome FROM audit_events_v1
                WHERE (? IS NULL OR client_id=?) AND (? IS NULL OR at>=?) AND (? IS NULL OR at<?)
                ORDER BY seq LIMIT ? OFFSET ?""",
                (client_id, client_id, start, _iso(start) if start is not None else None,
                 end, _iso(end) if end is not None else None, limit + 1, offset)).fetchall()
            records = tuple(AuditRecord(int(row["seq"]), row["at"], row["client_id"], row["entry"],
                row["action"], row["target"], row["outcome"]) for row in rows[:limit])
            return AuditPage(records, offset + limit if len(rows) > limit else None)

    def handshake(self, access: Access) -> dict[str, object]:
        identity = self._authorize(access)
        return {"contract_version": 1, "client_id": identity.client_id, "entry": identity.entry,
                "durable_text_streams": 1,
                "ordinary_text": True, "important": True, "changes": True, "captures": True, "storage_version": 5,
                "lexical": True, "lexical_normalization_version": NORMALIZATION_IDENTITY,
                "archive_review": identity.archive_review, "scopes": sorted(identity.scopes),
                "vault": False, "backup": "deferred", "sqlite_version": sqlite3.sqlite_version}

    def export_archive(self, access: Access, destination: Path) -> ArchiveStatus:
        from .archive import export_archive

        return export_archive(self, access, destination)

    def access_from_file(self, access_path: Path) -> Access:
        """Trusted local launcher bootstrap; never register this method as a model-facing tool."""
        access_path = external(access_path)
        if access_path.parent != self.root:
            raise MemoryFailure("invalid_access_location")
        verify_private_directory(self.root, extra_files=(access_path,))
        access = _access_record(_load(access_path))
        self._authorize(access)
        return access

    @contextmanager
    def _connection(self, *, check: Callable[[], float] | None = None) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        interrupted: list[MemoryFailure] = []
        try:
            if check is not None:
                check()
            connection = sqlite3.connect(self.root / "memory.sqlite3", timeout=0 if check else self._timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            if check is not None:
                def progress() -> int:
                    try:
                        check()
                        return 0
                    except MemoryFailure as failure:
                        interrupted.append(failure)
                        return 1
                connection.set_progress_handler(progress, 1000)
            connection.execute("PRAGMA foreign_keys=ON")
            setup_deadline = time.monotonic() + self._timeout
            while True:
                if check is not None:
                    check()
                try:
                    connection.execute("PRAGMA synchronous=FULL")
                    break
                except sqlite3.OperationalError as error:
                    # Cancellable readers use timeout=0. Even this connection-local
                    # setting can meet a short WAL lock; wait only before yielding.
                    if check is None or (error.sqlite_errorcode & 255) != sqlite3.SQLITE_BUSY:
                        raise
                    remaining = min(check(), setup_deadline - time.monotonic())
                    if remaining <= 0:
                        raise
                    time.sleep(min(.01, remaining))
            yield connection
            if interrupted:
                raise interrupted[0]
            if check is not None:
                check()
        except sqlite3.Error as error:
            if interrupted:
                raise interrupted[0] from None
            code = getattr(error, "sqlite_errorcode", 0) & 255
            raise MemoryFailure({sqlite3.SQLITE_BUSY: "storage_busy", sqlite3.SQLITE_LOCKED: "storage_busy", sqlite3.SQLITE_FULL: "storage_full"}.get(code, "storage_unavailable")) from None
        finally:
            if connection is not None:
                connection.close()

    def submit(self, access: Access, event: Event) -> Receipt:
        self._validate_event(event)
        identity = self._require_scope(access, "capture", "submit", _json(asdict(event.source)))
        if event.source.entry != identity.entry:
            raise MemoryFailure("unauthorized_source")
        fingerprint = hmac.digest(self._fingerprint_key, _json(asdict(event)).encode(), hashlib.sha256)
        retained = replace(event, text="[PROTECTED:ORIGINAL_NOT_SAVED]") if event.sensitive or event.protected_source or credential_signal(event.text) else event
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = self._persist_event(db, event, retained, fingerprint)
            db.execute("COMMIT")
        return receipt

    @staticmethod
    def _persist_event(db: sqlite3.Connection, event: Event, retained: Event, fingerprint: bytes,
                       *, stream_id: str | None = None) -> Receipt:
        source = _json(asdict(event.source))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='purge_targets_v1'").fetchone():
            marker_source = _json(asdict(_stream_marker_source(event.source)))
            if db.execute("""SELECT 1 FROM purge_targets_v1 t JOIN purge_requests_v1 r ON r.request_id=t.request_id
                WHERE t.source IN (?,?) AND r.state IN ('blocked','completed')""", (source, marker_source)).fetchone():
                raise MemoryFailure("source_purged")
        existing = db.execute("SELECT fingerprint, receipt FROM messages WHERE source=?", (source,)).fetchone()
        if existing is not None:
            if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                raise MemoryFailure("source_conflict")
            return Receipt(**json.loads(existing["receipt"]))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='text_streams_v1'").fetchone():
            reserved = db.execute("SELECT message_id FROM text_streams_v1 WHERE source=?", (source,)).fetchone()
            if reserved is not None and reserved[0] != stream_id:
                raise MemoryFailure("source_reserved_for_stream")
        projected = retained is not event
        receipt = Receipt(str(uuid4()), datetime.now(timezone.utc).isoformat(), "projection_only" if projected else "committed", "projection" if projected else "original", not projected)
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (receipt.message_id, source, fingerprint, _json(asdict(retained)), _json(asdict(receipt)), _time(event.occurred_at) if event.occurred_at is not None else None, receipt.received_at))
        db.execute("INSERT INTO jobs VALUES (?,?)", (receipt.message_id, "pending"))
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

    def append_stream(self, access: Access, event: Event, *, index: int, carry: str, input_id: str) -> "StreamReceipt":
        from .durable_stream import append
        return append(self, access, event, index=index, carry=carry, input_id=input_id)

    def read_stream(self, access: Access, source: Source, *, cursor: int = 0, limit: int = 2048) -> "StreamPage":
        from .durable_stream import read
        return read(self, access, source, cursor=cursor, limit=limit)

    def submit_stream(self, access: Access, event: Event, frames: tuple[dict[str, Any], ...]) -> CaptureReceipt:
        """Commit one buffered assistant stream before a trusted host publishes its frames."""
        self._validate_event(event)
        self._require_scope(access, "capture", "submit_stream", _json(asdict(event.source)))
        if event.role != "assistant" or not isinstance(frames, tuple) or not 1 <= len(frames) <= 65536:
            raise MemoryFailure("invalid_stream")
        if any(not isinstance(frame, dict) or not isinstance(frame.get("type"), str) for frame in frames):
            raise MemoryFailure("invalid_stream")
        try:
            raw = _json(frames).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError):
            raise MemoryFailure("invalid_stream") from None
        if len(raw) > 1048576:
            raise MemoryFailure("stream_too_large")
        if event.completion == "complete" and frames[-1]["type"] != "finish":
            raise MemoryFailure("incomplete_stream")
        fragments: dict[str, list[str]] = {}
        visible: dict[int, str] = {}
        for frame in frames:
            # Group deltas by logical block so interleaving cannot hide a split credential.
            index = frame.get("index", 0)
            if type(index) is not int or not 0 <= index <= 65536:
                raise MemoryFailure("invalid_stream")
            pieces = fragments.setdefault(str(index), [])
            for name in ("text", "delta", "arguments", "argumentsDelta", "partialJson"):
                if isinstance(frame.get(name), str):
                    pieces.append(frame[name])
            if frame["type"] == "text-delta":
                if not isinstance(frame.get("text"), str):
                    raise MemoryFailure("invalid_stream")
                visible[index] = visible.get(index, "") + frame["text"]
            if frame["type"] == "block-end" and isinstance(frame.get("block"), dict) and frame["block"].get("type") == "text":
                if not isinstance(frame["block"].get("text"), str):
                    raise MemoryFailure("invalid_stream")
                visible[index] = frame["block"]["text"]
        if event.text != "".join(visible.values()):
            raise MemoryFailure("stream_text_mismatch")
        protected = credential_signal(raw.decode("utf-8")) or any(credential_signal("".join(values)) for values in fragments.values())
        retained = replace(event, protected_source=event.protected_source or protected or event.completion != "complete")
        return self.submit_capture(access, Capture(retained, "message", attachments=(AttachmentInput(
            "raw-stream-v1", "raw-stream.json", "application/json", (raw,), expected_size=len(raw)),)))

    @staticmethod
    def _stored(row: sqlite3.Row) -> StoredMessage:
        value = json.loads(row["event"])
        value["source"] = Source(**value["source"])
        return StoredMessage(Event(**value), Receipt(**json.loads(row["receipt"])))

    def read(self, access: Access, message_id: str) -> StoredMessage | None:
        self._require_scope(access, "recall", "read")
        _identifier(message_id)
        with self._connection() as db:
            row = db.execute(f"SELECT event,receipt FROM messages WHERE id=? AND {_RECALLABLE}", (message_id,)).fetchone()
            return self._stored(row) if row is not None else None

    def read_source(self, access: Access, source: Source) -> StoredMessage | None:
        self._require_scope(access, "recall", "read_source")
        if not isinstance(source, Source):
            raise MemoryFailure("invalid_source")
        for value in asdict(source).values():
            _identifier(value)
        with self._connection() as db:
            row = db.execute(f"SELECT event,receipt FROM messages WHERE source=? AND {_RECALLABLE}", (_json(asdict(source)),)).fetchone()
            return self._stored(row) if row is not None else None

    def list_messages(self, access: Access, *, since: str | None = None, until: str | None = None, limit: int = 100, offset: int = 0) -> Page:
        self._require_scope(access, "recall", "list_messages")
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
        self._require_scope(access, "maintain", "mark_important")
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
        self._require_scope(access, "recall", "read_important")
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
        self._require_scope(access, "recall", "state")
        _identifier(message_id)
        with self._connection() as db:
            return self._state(db, message_id)

    def change(self, access: Access, request: Change) -> MessageState:
        self._require_scope(access, "maintain", "change")
        if not isinstance(request, Change) or type(request.expected_version) is not int or request.expected_version < 1:
            raise MemoryFailure("invalid_change")
        for value in [request.request_id, request.target_id, request.evidence_id, request.reason]:
            _identifier(value)
        if not request.reason.strip() or not isinstance(request.action, str) or request.action not in {"correct", "revoke", "suppress"}:
            raise MemoryFailure("invalid_change")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._change(db, request)
            db.execute("COMMIT")
        return state

    def _change(self, db: sqlite3.Connection, request: Change) -> MessageState:
        """Apply a validated change in the caller's transaction (also used by trusted UI confirmation)."""
        fingerprint = hmac.digest(self._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        existing = db.execute("SELECT fingerprint,receipt FROM changes WHERE request_id=?", (request.request_id,)).fetchone()
        if existing is not None:
            if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                raise MemoryFailure("change_conflict")
            return MessageState(**json.loads(existing["receipt"]))
        current = self._state(db, request.target_id)
        if current.version != request.expected_version:
            raise MemoryFailure("version_conflict")
        if current.status == "purged":
            # Phase-1 purge scope is frozen: no change may relabel a target already marked purged.
            raise MemoryFailure("not_recallable")
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
        return state

    def review_archive(self, access: Access, message_id: str) -> ArchiveView:
        identity = self._authorize(access)
        if not identity.archive_review:
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
        self._require_scope(access, "capture", "publish_context")
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

    @staticmethod
    def _capture_receipt(encoded: str) -> CaptureReceipt:
        value = json.loads(encoded)
        value["message"] = Receipt(**value["message"])
        value["attachments"] = tuple(AttachmentInfo(**item) for item in value["attachments"])
        value["gaps"] = tuple(value["gaps"])
        return CaptureReceipt(**value)

    def submit_capture(self, access: Access, capture: Capture) -> CaptureReceipt:
        identity = self._require_scope(access, "capture", "submit_capture",
            _json(asdict(capture.event.source)) if isinstance(capture, Capture) and isinstance(capture.event, Event) else "")
        if not isinstance(capture, Capture):
            raise MemoryFailure("invalid_capture")
        event = capture.event
        stream_fingerprint: str | None = None
        stream_oversize = False
        if capture.text_chunks is not None:
            if (not isinstance(event, Event) or event.text or not isinstance(capture.text_chunks, tuple)
                    or len(capture.text_chunks) > 4096 or any(not isinstance(chunk, str) for chunk in capture.text_chunks)):
                raise MemoryFailure("invalid_capture")
            self._validate_event(event)
            stream_oversize = sum(len(chunk) for chunk in capture.text_chunks) > 1048576
            keyed_stream = hmac.new(self._fingerprint_key, digestmod=hashlib.sha256)
            for chunk in capture.text_chunks:
                keyed_stream.update(chunk.encode("utf-8", errors="surrogatepass"))
            stream_fingerprint = keyed_stream.hexdigest()
            event = replace(event, text="" if stream_oversize else "".join(capture.text_chunks),
                            protected_source=event.protected_source or event.completion != "complete")
        body_gap: str | None = "size_limit" if stream_oversize else None
        try:
            self._validate_event(event)
        except MemoryFailure as error:
            if error.code not in {"message_too_large", "invalid_encoding"}:
                raise
            self._validate_event(replace(event, text=""))
            body_gap = "size_limit" if error.code == "message_too_large" else "invalid_encoding"
        if event.source.entry != identity.entry:
            raise MemoryFailure("unauthorized_source")
        if not isinstance(capture.kind, str) or capture.kind not in {"message", "tool_call", "tool_result"} or type(capture.provided) is not bool or not isinstance(capture.attachments, tuple) or len(capture.attachments) > 16:
            raise MemoryFailure("invalid_capture")
        if not capture.provided:
            if event.text:
                raise MemoryFailure("invalid_capture")
            body_gap = "entry_not_provided"
        if capture.input_id is not None:
            _identifier(capture.input_id)
        if capture.tool_name is not None:
            _identifier(capture.tool_name)
        if capture.kind != "message" and (capture.input_id is None or capture.tool_name is None):
            raise MemoryFailure("invalid_capture")
        descriptors: list[dict[str, object]] = []
        infos: list[AttachmentInfo] = []
        objects: list[tuple[str, bytes, str]] = []
        protected = event.sensitive or event.protected_source or credential_signal(event.text)
        attachment_ids: set[str] = set()
        for attachment in capture.attachments:
            if not isinstance(attachment, AttachmentInput):
                raise MemoryFailure("invalid_attachment")
            _identifier(attachment.attachment_id)
            _identifier(attachment.label)
            _identifier(attachment.media_type)
            if attachment.attachment_id in attachment_ids:
                raise MemoryFailure("duplicate_attachment")
            attachment_ids.add(attachment.attachment_id)
            if (type(attachment.complete) is not bool or type(attachment.protected) is not bool
                    or (attachment.expected_size is not None and (type(attachment.expected_size) is not int or attachment.expected_size < 0))
                    or (attachment.expected_sha256 is not None and (not isinstance(attachment.expected_sha256, str)
                        or len(attachment.expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in attachment.expected_sha256)))
                    or (attachment.chunks is not None and (not isinstance(attachment.chunks, tuple)
                        or any(not isinstance(chunk, bytes) for chunk in attachment.chunks)))):
                raise MemoryFailure("invalid_attachment")
            chunks = attachment.chunks or ()
            size = sum(len(chunk) for chunk in chunks)
            keyed = hmac.new(self._fingerprint_key, digestmod=hashlib.sha256)
            for byte_chunk in chunks:
                keyed.update(byte_chunk)
            descriptors.append({"id": attachment.attachment_id, "label": attachment.label,
                "media_type": attachment.media_type, "fingerprint": keyed.hexdigest(), "provided": attachment.chunks is not None,
                "expected_size": attachment.expected_size, "expected_sha256": attachment.expected_sha256,
                "complete": attachment.complete, "protected": attachment.protected})
            gap: str | None = None
            if protected or attachment.protected:
                gap = "protected_original_not_saved"
            elif attachment.chunks is None:
                gap = "entry_not_provided"
            elif not attachment.complete:
                gap = "incomplete_attachment"
            elif size > 1048576:
                gap = "size_limit"
            elif attachment.media_type not in {"text/plain", "text/markdown", "application/json"}:
                gap = "unsupported_format"
            data = b"" if gap else b"".join(chunks)
            if gap is None:
                try:
                    text = data.decode("utf-8", errors="strict")
                    if credential_signal(text):
                        gap = "protected_original_not_saved"
                except UnicodeError:
                    gap = "invalid_encoding"
            digest = hashlib.sha256(data).hexdigest() if gap is None else None
            if gap is None and ((attachment.expected_size is not None and attachment.expected_size != size)
                    or (attachment.expected_sha256 is not None and attachment.expected_sha256 != digest)):
                gap, digest = "integrity_mismatch", None
            infos.append(AttachmentInfo(attachment.attachment_id, attachment.label, attachment.media_type,
                                        size if attachment.chunks is not None else None, digest, gap is None, gap))
            if gap is not None:
                continue
            assert digest is not None
            objects.append((attachment.attachment_id, data, digest))
        fingerprint = hmac.digest(self._fingerprint_key, _json({"event": asdict(event), "kind": capture.kind,
            "input_id": capture.input_id, "tool_name": capture.tool_name, "provided": capture.provided,
            "stream_fingerprint": stream_fingerprint, "attachments": descriptors}).encode("utf-8", errors="surrogatepass"), hashlib.sha256)
        retained = replace(event, text="[PROTECTED:ORIGINAL_NOT_SAVED]") if protected else event
        if body_gap is not None:
            retained = replace(event, text=f"[CAPTURE_GAP:{body_gap}]")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if capture.input_id is not None:
                self._state(db, capture.input_id)
            receipt = self._persist_event(db, event, retained, fingerprint)
            existing = db.execute("SELECT manifest FROM captures WHERE message_id=?", (receipt.message_id,)).fetchone()
            if existing is not None:
                return self._check_capture(db, self._capture_receipt(existing["manifest"]))
            # A new capture must not depend on a message that is no longer recallable; otherwise a blocked
            # purge could never finish because the late dependent would fail the scope recheck forever.
            if capture.input_id is not None and self._state(db, capture.input_id).status != "active":
                raise MemoryFailure("not_recallable")
            gaps = tuple(f"attachment:{info.attachment_id}:{info.gap}" for info in infos if info.gap is not None)
            if protected:
                gaps += ("protected_original_not_saved",)
            if body_gap is not None:
                gaps += (body_gap,)
            if event.completion != "complete":
                gaps += ("incomplete_output",)
            result = CaptureReceipt(receipt, capture.kind, capture.input_id, capture.tool_name, tuple(infos), gaps, not gaps)
            db.execute("INSERT INTO captures VALUES (?,?,?)", (receipt.message_id, capture.input_id, _json(asdict(result))))
            for attachment_id, data, digest in objects:
                db.execute("INSERT INTO attachments VALUES (?,?,?,?)", (receipt.message_id, attachment_id, data, digest))
            db.execute("COMMIT")
        return result

    def read_capture(self, access: Access, message_id: str) -> CaptureReceipt | None:
        self._require_scope(access, "recall", "read_capture")
        _identifier(message_id)
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute(f"""SELECT c.manifest FROM captures c JOIN messages ON messages.id=c.message_id
                WHERE c.message_id=? AND {_RECALLABLE}""", (message_id,)).fetchone()
            if row is None:
                return None
            receipt = self._capture_receipt(row["manifest"])
            return self._check_capture(db, receipt)

    def _check_capture(self, db: sqlite3.Connection, receipt: CaptureReceipt) -> CaptureReceipt:
        infos: list[AttachmentInfo] = []
        gaps = list(receipt.gaps)
        for info in receipt.attachments:
            if info.saved:
                try:
                    self._attachment_data(db, receipt.message.message_id, info)
                except MemoryFailure as error:
                    info = replace(info, saved=False, gap=error.code)
                    gaps.append(f"attachment:{info.attachment_id}:{error.code}")
            infos.append(info)
        return replace(receipt, attachments=tuple(infos), gaps=tuple(gaps), complete=not gaps)

    @staticmethod
    def _attachment_data(db: sqlite3.Connection, message_id: str, info: AttachmentInfo) -> bytes:
        row = db.execute("SELECT content,sha256 FROM attachments WHERE message_id=? AND attachment_id=?", (message_id, info.attachment_id)).fetchone()
        if row is None:
            raise MemoryFailure("attachment_missing")
        data = bytes(row["content"])
        if len(data) != info.size or row["sha256"] != info.sha256 or hashlib.sha256(data).hexdigest() != info.sha256:
            raise MemoryFailure("attachment_corrupt")
        return data

    def read_attachment(self, access: Access, message_id: str, attachment_id: str) -> bytes | None:
        self._require_scope(access, "recall", "read_attachment")
        _identifier(message_id)
        _identifier(attachment_id)
        with self._connection() as db:
            db.execute("BEGIN")
            row = db.execute(f"""SELECT c.manifest FROM captures c JOIN messages ON messages.id=c.message_id
                WHERE c.message_id=? AND {_RECALLABLE}""", (message_id,)).fetchone()
            if row is None:
                return None
            receipt = self._capture_receipt(row["manifest"])
            info = next((item for item in receipt.attachments if item.attachment_id == attachment_id), None)
            return self._attachment_data(db, message_id, info) if info is not None and info.saved else None

    def search(self, access: Access, query: str, *, mode: str = "lexical", since: str | None = None,
               until: str | None = None, limit: int = 10, offset: int = 0, timeout: float = 30,
               cancel: threading.Event | None = None, current_input_id: str | None = None) -> SearchPage:
        self._require_scope(access, "recall", "search")
        if current_input_id is not None:
            _identifier(current_input_id)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise MemoryFailure("invalid_timeout")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise MemoryFailure("invalid_cancel")
        deadline = time.monotonic() + timeout
        def budget() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure("search_canceled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryFailure("search_timeout")
            return remaining
        budget()
        if (not isinstance(query, str) or not query.strip() or len(query) > 256
                or not isinstance(mode, str) or mode not in {"exact", "lexical"}):
            raise MemoryFailure("invalid_query")
        try:
            query.encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if credential_signal(query):
            raise MemoryFailure("protected_query")
        words = tuple(dict.fromkeys(normalize(word) for word in ([query] if mode == "exact" else query.split())))
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_page")
        start, end = _time(since) if since is not None else None, _time(until) if until is not None else None
        if start is not None and end is not None and start >= end:
            raise MemoryFailure("invalid_time_range")
        with self._connection(check=budget) as db:
            db.execute("BEGIN")
            status, indexed_query = self._search_index(db, budget)
            base = f"""SELECT messages.event,messages.receipt,COALESCE(v.version,1) AS state_version
                FROM messages LEFT JOIN visibility v ON v.message_id=messages.id
                WHERE (? IS NULL OR occurred>=?) AND (? IS NULL OR occurred<?) AND {_RECALLABLE}
                  AND (? IS NULL OR messages.id<>?)"""
            order = " ORDER BY occurred,received,messages.id"
            parameters: tuple[object, ...] = (start, start, end, end, current_input_id, current_input_id)
            used_index = status.state in {"ready", "behind"}
            reason = {"not_built": "index_not_built", "unavailable": "index_unavailable", "behind": "index_behind"}.get(status.state)
            try:
                condition = f" AND (messages.id IN (SELECT message_id FROM lexical WHERE lexical MATCH ?) OR messages.id NOT IN ({indexed_query}))" if used_index else ""
                hits = self._search_hits(db, base + condition + order,
                    parameters + (query_expression(words),) if used_index else parameters, words, limit, offset, check=budget)
            except sqlite3.Error:
                budget()
                if not used_index:
                    raise
                used_index, reason = False, "index_unavailable"
                status = replace(status, state="unavailable", indexed_messages=0,
                                 pending_messages=status.source_messages, indexed_watermark=None)
                hits = self._search_hits(db, base + order, parameters, words, limit, offset, check=budget)
            budget()
            return SearchPage(tuple(hits[:limit]), offset + limit if len(hits) > limit else None, status, used_index, reason)

    def _search_hits(self, db: sqlite3.Connection, sql: str, parameters: tuple[object, ...],
                     words: tuple[str, ...], limit: int, offset: int,
                     check: Callable[[], object] | None = None) -> list[SearchHit]:
        hits: list[SearchHit] = []
        found = 0
        for row in db.execute(sql, parameters):
            if check is not None:
                check()
            message = self._stored(row)
            spans = matching_spans(message.event.text, words, check=check)
            if not spans:
                continue
            if found >= offset:
                hits.append(SearchHit(message.receipt.message_id, message.event.source, message.event.role,
                    message.event.sequence, message.event.occurred_at, message.receipt.received_at,
                    message.event.completion, message.receipt.representation, message.receipt.content_version,
                    int(row["state_version"]), spans))
            found += 1
            if len(hits) > limit:
                break
        return hits

    @staticmethod
    def _lexical_status(db: sqlite3.Connection, *, materialized: bool = False) -> LexicalStatus:
        counts = db.execute(f"SELECT count(*),max(received) FROM messages WHERE {_RECALLABLE}").fetchone()
        source = int(counts[0])
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name IN ('lexical','lexical_docs')")}
        if len(present) != 2:
            return LexicalStatus("not_built", source, 0, source, counts[1], None)
        try:
            if materialized:
                indexed = db.execute("SELECT count(*),max(received) FROM temp.evernook_search_valid_v1").fetchone()
            else:
                indexed = None
                if MemoryRuntime._lexical_acceleration_valid(db):
                    try:
                        indexed = db.execute(f"SELECT count(*),max(messages.received) {_LEXICAL_ACCELERATED_ROWS} AND {_RECALLABLE}").fetchone()
                    except sqlite3.Error:
                        if not db.in_transaction:
                            raise
                if indexed is None:
                    indexed = db.execute(f"SELECT count(*),max(messages.received) {_LEXICAL_VALID_ROWS} AND {_RECALLABLE}").fetchone()
        except sqlite3.Error:
            return LexicalStatus("unavailable", source, 0, source, counts[1], None)
        covered = int(indexed[0])
        return LexicalStatus("ready" if covered == source else "behind", source, covered, source - covered, counts[1], indexed[1])

    @staticmethod
    def _lexical_acceleration_valid(db: sqlite3.Connection) -> bool:
        row = db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=? COLLATE NOCASE", (_LEXICAL_ACCELERATION_INDEX,)).fetchone()
        return (row is not None and row[0] == "index" and row[1] == "messages"
            and row[2] == _LEXICAL_ACCELERATION_SQL)

    def lexical_acceleration_enabled(self, access: Access) -> bool:
        self._authorize(access)
        with self._connection() as db:
            return self._lexical_acceleration_valid(db)

    def configure_lexical_acceleration(self, access: Access, *, enabled: bool) -> bool:
        self._require_scope(access, "maintain", "configure_lexical_acceleration")
        if type(enabled) is not bool:
            raise MemoryFailure("invalid_lexical_acceleration")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (_LEXICAL_ACCELERATION_INDEX,)).fetchone()
            if exists and not self._lexical_acceleration_valid(db):
                raise MemoryFailure("lexical_acceleration_conflict")
            if enabled and not exists:
                db.execute(_LEXICAL_ACCELERATION_SQL)
            elif not enabled and exists:
                db.execute(f"DROP INDEX {_LEXICAL_ACCELERATION_INDEX}")
            db.execute("COMMIT")
        return enabled

    def _search_index(self, db: sqlite3.Connection, check: Callable[[], object]) -> tuple[LexicalStatus, str]:
        if not self._lexical_acceleration_valid(db):
            return self._lexical_status(db), _LEXICAL_INDEXED
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name IN ('lexical','lexical_docs')")}
        if len(present) != 2:
            return self._lexical_status(db), _LEXICAL_INDEXED
        saved = False
        try:
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("SAVEPOINT evernook_search_acceleration")
            saved = True
            db.execute("CREATE TEMP TABLE evernook_search_valid_v1 (message_id TEXT PRIMARY KEY, received TEXT NOT NULL)")
            db.execute(f"INSERT INTO temp.evernook_search_valid_v1 SELECT d.message_id,messages.received "
                f"{_LEXICAL_ACCELERATED_ROWS} AND {_RECALLABLE}")
            check()
            status = self._lexical_status(db, materialized=True)
            db.execute("RELEASE evernook_search_acceleration")
            return status, "SELECT message_id FROM temp.evernook_search_valid_v1"
        except sqlite3.Error:
            check()
            if not db.in_transaction:
                raise
            if saved:
                db.execute("ROLLBACK TO evernook_search_acceleration")
                db.execute("RELEASE evernook_search_acceleration")
            return self._lexical_status(db), _LEXICAL_INDEXED

    def lexical_status(self, access: Access) -> LexicalStatus:
        self._authorize(access)
        with self._connection() as db:
            db.execute("BEGIN")
            return self._lexical_status(db)

    @staticmethod
    def _lexical_schema(db: sqlite3.Connection) -> None:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(message_id UNINDEXED,terms)")
        db.execute("""CREATE TABLE IF NOT EXISTS lexical_docs (
            message_id TEXT PRIMARY KEY REFERENCES messages(id), content_version INTEGER NOT NULL,
            normalization_version INTEGER NOT NULL)""")

    @staticmethod
    def _lexical_pending(db: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
        # The caller holds one read snapshot across index validation and selection.
        if MemoryRuntime._lexical_acceleration_valid(db):
            try:
                return db.execute(f"""SELECT event,receipt FROM messages JOIN (
                    SELECT id FROM messages INDEXED BY {_LEXICAL_ACCELERATION_INDEX}
                    WHERE {_RECALLABLE} AND id NOT IN (SELECT d.message_id {_LEXICAL_ACCELERATED_ROWS})
                    ORDER BY received,id LIMIT ?) selected ON messages.id=selected.id
                    ORDER BY messages.received,messages.id""", (limit,)).fetchall()
            except sqlite3.Error:
                if not db.in_transaction:
                    raise
        return db.execute(f"""SELECT event,receipt FROM messages
            WHERE {_RECALLABLE} AND id NOT IN ({_LEXICAL_INDEXED}) ORDER BY received,id LIMIT ?""", (limit,)).fetchall()

    def refresh_lexical(self, access: Access, *, limit: int = 100) -> LexicalStatus:
        self._require_scope(access, "maintain", "refresh_lexical")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryFailure("invalid_page")
        prepared = False
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                self._lexical_schema(db)
                db.execute("COMMIT")
                db.execute("BEGIN")
                rows = self._lexical_pending(db, limit)
                db.execute("COMMIT")
                prepared = True
                # Sample before the lookup, and compare only on this same connection.
                # A peer commit invalidates the optimization, including equal-version
                # FTS replacements or a rebuild that reuses old rowids.
                version = int(db.execute("PRAGMA data_version").fetchone()[0])
                rowids = self._lexical_rowids(db, rows)
                for row in rows:
                    message = self._stored(row)
                    terms = index_terms(message.event.text)  # No write transaction during derivation.
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(f"SELECT event,receipt FROM messages WHERE id=? AND {_RECALLABLE}", (message.receipt.message_id,)).fetchone()
                    if current is None or current["event"] != row["event"] or current["receipt"] != row["receipt"]:
                        db.execute("ROLLBACK")
                        continue
                    if rowids is not None and int(db.execute("PRAGMA data_version").fetchone()[0]) != version:
                        rowids = None
                    if rowids is None:
                        db.execute("DELETE FROM lexical WHERE message_id=?", (message.receipt.message_id,))
                    else:
                        db.executemany("DELETE FROM lexical WHERE rowid=? AND message_id=?",
                            ((rowid, message.receipt.message_id) for rowid in rowids.get(message.receipt.message_id, ())))
                    db.execute("INSERT INTO lexical VALUES (?,?)", (message.receipt.message_id, terms))
                    db.execute("INSERT OR REPLACE INTO lexical_docs VALUES (?,?,?)", (message.receipt.message_id, message.receipt.content_version, NORMALIZATION_IDENTITY))
                    db.execute("COMMIT")
        except MemoryFailure as error:
            if prepared or error.code != "storage_unavailable":
                raise
            return replace(self.lexical_status(access), state="unavailable")
        return self.lexical_status(access)

    @staticmethod
    def _lexical_rowids(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, list[int]] | None:
        try:
            ids = tuple(Receipt(**json.loads(row["receipt"])).message_id for row in rows)
        except (KeyError, TypeError, ValueError):
            # Preserve normal source-order failures rather than parsing later bad
            # receipts before any earlier document has been processed.
            return None
        if not ids:
            return {}
        if any(type(message_id) is not str for message_id in ids):
            return None
        found: dict[str, list[int]] = {}
        try:
            with closing(db.execute(f"SELECT rowid,message_id FROM lexical WHERE message_id IN ({','.join('?' for _ in ids)})", ids)) as cursor:
                for count, row in enumerate(cursor):
                    if count >= _LEXICAL_ROWID_CACHE_LIMIT:
                        return None
                    found.setdefault(row[1], []).append(int(row[0]))
        except sqlite3.Error:
            return None
        return found

    def rebuild_lexical(self, access: Access, *, limit: int = 100) -> LexicalStatus:
        self._require_scope(access, "maintain", "rebuild_lexical")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryFailure("invalid_page")
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DROP TABLE IF EXISTS lexical")
                db.execute("DROP TABLE IF EXISTS lexical_docs")
                self._lexical_schema(db)
                db.execute("COMMIT")
        except MemoryFailure as error:
            if error.code != "storage_unavailable":
                raise
            return replace(self.lexical_status(access), state="unavailable")
        return self.refresh_lexical(access, limit=limit)
