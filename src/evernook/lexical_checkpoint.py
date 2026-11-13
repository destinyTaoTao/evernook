"""Explicit, rebuildable lexical coverage checkpoints; never an authority for originals."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

from .lexical import NORMALIZATION_IDENTITY
from .models import LexicalStatus, MemoryFailure
from .runtime import MemoryRuntime, _LEXICAL_INDEXED, _RECALLABLE


_PREFIX = "evernook_checkpoint_v1_"
_ROWS = _PREFIX + "rows"
_DIRTY = _PREFIX + "dirty"
_HEAD = _PREFIX + "head"
_RECEIVED = _PREFIX + "received"
_VALID = _PREFIX + "current_valid"
_PENDING = _PREFIX + "current_pending"
_QUERY_DIRTY_LIMIT = 100


def _definitions() -> dict[str, tuple[str, str, str]]:
    result = {
        _ROWS: ("table", _ROWS, f"CREATE TABLE {_ROWS}(message_id TEXT PRIMARY KEY,received TEXT NOT NULL,indexed INTEGER NOT NULL CHECK(indexed IN (0,1))) WITHOUT ROWID"),
        _DIRTY: ("table", _DIRTY, f"CREATE TABLE {_DIRTY}(message_id TEXT PRIMARY KEY) WITHOUT ROWID"),
        _HEAD: ("table", _HEAD, f"CREATE TABLE {_HEAD}(singleton INTEGER PRIMARY KEY CHECK(singleton=1),format_version INTEGER NOT NULL CHECK(format_version=1),normalization_version INTEGER NOT NULL,policy_identity TEXT NOT NULL,source_identity TEXT NOT NULL,schema_version INTEGER NOT NULL,source_count INTEGER NOT NULL CHECK(source_count>=0),indexed_count INTEGER NOT NULL CHECK(indexed_count>=0 AND indexed_count<=source_count))"),
        _RECEIVED: ("index", _ROWS, f"CREATE INDEX {_RECEIVED} ON {_ROWS}(indexed,received)"),
    }
    for table, column in (("messages", "id"), ("visibility", "message_id"), ("lexical_docs", "message_id"), ("lexical_content", "c0")):
        for action, references in (("insert", ("NEW",)), ("delete", ("OLD",)), ("update", ("OLD", "NEW"))):
            name = _PREFIX + "track_" + table + "_" + action
            body = "".join(f"INSERT OR IGNORE INTO {_DIRTY} SELECT {ref}.{column} WHERE {ref}.{column} IS NOT NULL;" for ref in references)
            result[name] = ("trigger", table, f"CREATE TRIGGER {name} AFTER {action.upper()} ON {table} BEGIN {body} END")
    return result


_DEFINITIONS = _definitions()
_POLICY_IDENTITY = hashlib.sha256(
    Path(__file__).read_bytes() + Path(__file__).with_name("lexical.py").read_bytes()
    + (_LEXICAL_INDEXED + _RECALLABLE).encode()).hexdigest()


def _owned(db: sqlite3.Connection) -> set[str]:
    found = set()
    for row in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE lower(name) GLOB ?", (_PREFIX + "*",)):
        name = row[0]
        if name not in _DEFINITIONS or tuple(row[1:]) != _DEFINITIONS[name]:
            raise MemoryFailure("lexical_checkpoint_conflict")
        found.add(name)
    return found


def _source_identity(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name IN ('messages','visibility','lexical','lexical_docs','lexical_content') ORDER BY name").fetchall()
    return hashlib.sha256(repr([tuple(row) for row in rows]).encode()).hexdigest()


def _head(db: sqlite3.Connection, check: Callable[[], object] | None = None) -> tuple[int, int, str | None, str | None] | None:
    if _owned(db) != set(_DEFINITIONS) or MemoryRuntime._lexical_id_format(db) is not True:
        return None
    row = db.execute(f"SELECT format_version,normalization_version,policy_identity,source_identity,schema_version,source_count,indexed_count FROM {_HEAD} WHERE singleton=1").fetchone()
    if row is None or tuple(row[:4]) != (1, NORMALIZATION_IDENTITY, _POLICY_IDENTITY, _source_identity(db)):
        return None
    if row[4] != db.execute("PRAGMA schema_version").fetchone()[0]:
        return None
    if any(type(value) is not int for value in (row[4], row[5], row[6])) or not 0 <= row[6] <= row[5]:
        return None
    # Check inexpensive derived rows, rather than trusting damaged cached counts.
    # These scans do not rejoin the original or FTS tables; their cost is measured separately.
    counts = db.execute(f"""SELECT count(*),coalesce(sum(indexed),0),coalesce(sum(CASE WHEN
        typeof(message_id)='text' AND typeof(received)='text' AND typeof(indexed)='integer'
        AND indexed IN (0,1) THEN 0 ELSE 1 END),0) FROM {_ROWS}""").fetchone()
    if check is not None:
        check()
    if tuple(counts) != (row[5], row[6], 0):
        return None
    if db.execute(f"SELECT 1 FROM {_DIRTY} WHERE typeof(message_id)<>'text' LIMIT 1").fetchone():
        return None
    watermarks: list[str | None] = []
    for flag in (0, 1):
        highest = db.execute(f"SELECT message_id,received FROM {_ROWS} WHERE indexed=? AND message_id NOT IN (SELECT message_id FROM {_DIRTY}) ORDER BY received DESC LIMIT 1", (flag,)).fetchone()
        if highest is not None:
            original = db.execute("SELECT received FROM messages WHERE id=?", (highest[0],)).fetchone()
            if original is None or original[0] != highest[1]:
                return None
        watermarks.append(str(highest[1]) if highest is not None else None)
    source_watermark = max((value for value in watermarks if value is not None), default=None)
    return int(row[5]), int(row[6]), source_watermark, watermarks[1]


def enabled(db: sqlite3.Connection) -> bool:
    try:
        return _head(db) is not None
    except (sqlite3.Error, MemoryFailure):
        return False


def _remove(db: sqlite3.Connection, found: set[str]) -> None:
    for kind in ("trigger", "index", "table"):
        for name in found:
            if _DEFINITIONS[name][0] == kind:
                db.execute(f"DROP {kind.upper()} {name}")


def configure(db: sqlite3.Connection, *, active: bool) -> bool:
    """Caller owns a trusted write transaction; unknown objects are never removed."""
    found = _owned(db)
    if not active:
        _remove(db, found)
        return False
    if MemoryRuntime._lexical_id_format(db) is not True:
        raise MemoryFailure("lexical_checkpoint_requires_id_acceleration")
    if _head(db) is not None:
        return True
    _remove(db, found)
    for kind, _table, sql in _DEFINITIONS.values():
        if kind != "trigger":
            db.execute(sql)
    db.execute(f"INSERT INTO {_ROWS} SELECT messages.id,messages.received,CASE WHEN messages.id IN ({_LEXICAL_INDEXED}) THEN 1 ELSE 0 END FROM messages WHERE {_RECALLABLE}")
    for kind, _table, sql in _DEFINITIONS.values():
        if kind == "trigger":
            db.execute(sql)
    counts = db.execute(f"SELECT count(*),coalesce(sum(indexed),0) FROM {_ROWS}").fetchone()
    db.execute(f"INSERT INTO {_HEAD} VALUES (1,1,?,?,?,?,?,?)", (NORMALIZATION_IDENTITY, _POLICY_IDENTITY,
        _source_identity(db), db.execute("PRAGMA schema_version").fetchone()[0], *counts))
    return True


def _current(db: sqlite3.Connection, message_id: str) -> tuple[str, int] | None:
    row = db.execute(f"""SELECT messages.received,d.content_version,d.normalization_version,
        json_extract(messages.receipt,'$.content_version') FROM messages
        LEFT JOIN lexical_docs d ON d.message_id=messages.id WHERE messages.id=? AND {_RECALLABLE}""", (message_id,)).fetchone()
    if row is None:
        return None
    valid = row[1] == row[3] and row[2] == NORMALIZATION_IDENTITY
    if valid:
        phrase = 'message_id:"' + message_id.replace('"', '""') + '"'
        valid = db.execute("SELECT 1 FROM lexical WHERE lexical MATCH ? AND message_id=? LIMIT 1", (phrase, message_id)).fetchone() is not None
    return str(row[0]), int(valid)


def refresh(db: sqlite3.Connection, *, limit: int) -> int:
    """Reconcile a bounded queue in the caller's single trusted write transaction."""
    counts = _head(db)
    if counts is None:
        raise MemoryFailure("lexical_checkpoint_invalid")
    source, indexed = counts[:2]
    ids = [str(row[0]) for row in db.execute(f"SELECT message_id FROM {_DIRTY} ORDER BY message_id LIMIT ?", (limit,))]
    for message_id in ids:
        old = db.execute(f"SELECT received,indexed FROM {_ROWS} WHERE message_id=?", (message_id,)).fetchone()
        current = _current(db, message_id)
        source += int(current is not None) - int(old is not None)
        indexed += (current[1] if current is not None else 0) - (int(old[1]) if old is not None else 0)
        db.execute(f"DELETE FROM {_ROWS} WHERE message_id=?", (message_id,))
        if current is not None:
            db.execute(f"INSERT INTO {_ROWS} VALUES (?,?,?)", (message_id, *current))
        db.execute(f"DELETE FROM {_DIRTY} WHERE message_id=?", (message_id,))
    db.execute(f"UPDATE {_HEAD} SET source_count=?,indexed_count=? WHERE singleton=1", (source, indexed))
    return len(ids)


def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    """Confirmed purge calls this after raw deletion, inside its phase-2 transaction."""
    found = _owned(db)
    if not found:
        return
    limit = max(1, min(100, db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)))
    for offset in range(0, len(ids), limit):
        selected = ids[offset:offset+limit]
        placeholders = ",".join("?" for _ in selected)
        for table in (_ROWS, _DIRTY):
            if table in found:
                db.execute(f"DELETE FROM {table} WHERE message_id IN ({placeholders})", selected)
    if _ROWS in found and _HEAD in found:
        counts = db.execute(f"SELECT count(*),coalesce(sum(indexed),0) FROM {_ROWS}").fetchone()
        db.execute(f"UPDATE {_HEAD} SET source_count=?,indexed_count=? WHERE singleton=1", tuple(counts))


def strip_export(db: sqlite3.Connection) -> None:
    """An unpublished export omits rebuildable tracking; unknown SQL stays rejected."""
    found = _owned(db)
    for row in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE type IN ('trigger','view')"):
        if row[0] not in found or row[1] != "trigger" or tuple(row[1:]) != _DEFINITIONS[row[0]]:
            raise MemoryFailure("archive_integrity_mismatch")
    _remove(db, found)


@dataclass(frozen=True)
class SearchState:
    status: LexicalStatus
    valid_query: str
    pending_query: str


def search_state(db: sqlite3.Connection, check: Callable[[], object]) -> SearchState | None:
    """Read-only snapshot check. Missing, unsupported or damaged derived state falls back."""
    saved = False
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE lower(name) GLOB ? LIMIT 1", (_PREFIX + "*",)).fetchone():
            return None
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("SAVEPOINT evernook_checkpoint_query")
        saved = True
        counts = _head(db, check)
        if counts is None:
            db.execute("ROLLBACK TO evernook_checkpoint_query")
            db.execute("RELEASE evernook_checkpoint_query")
            return None
        dirty_rows = db.execute(f"SELECT message_id FROM {_DIRTY} ORDER BY message_id LIMIT ?", (_QUERY_DIRTY_LIMIT + 1,)).fetchall()
        if any(type(row[0]) is not str for row in dirty_rows):
            raise sqlite3.DataError("invalid_checkpoint_dirty_id")
        ids = [str(row[0]) for row in dirty_rows]
        if len(ids) > _QUERY_DIRTY_LIMIT:
            db.execute("ROLLBACK TO evernook_checkpoint_query")
            db.execute("RELEASE evernook_checkpoint_query")
            return None
        source, indexed, source_watermark, indexed_watermark = counts
        db.execute(f"CREATE TEMP TABLE {_VALID}(message_id TEXT PRIMARY KEY) WITHOUT ROWID")
        db.execute(f"CREATE TEMP TABLE {_PENDING}(message_id TEXT PRIMARY KEY) WITHOUT ROWID")
        for message_id in ids:
            check()
            old = db.execute(f"SELECT received,indexed FROM {_ROWS} WHERE message_id=?", (message_id,)).fetchone()
            current = _current(db, message_id)
            source += int(current is not None) - int(old is not None)
            indexed += (current[1] if current is not None else 0) - (int(old[1]) if old is not None else 0)
            if current is not None:
                source_watermark = max(source_watermark or current[0], current[0])
                if current[1]:
                    indexed_watermark = max(indexed_watermark or current[0], current[0])
                db.execute(f"INSERT INTO temp.{_VALID if current[1] else _PENDING} VALUES (?)", (message_id,))
        if not 0 <= indexed <= source:
            raise sqlite3.DataError("invalid_checkpoint_counts")
        check()
        db.execute("RELEASE evernook_checkpoint_query")
        status = LexicalStatus("ready" if source == indexed else "behind", source, indexed, source-indexed,
            source_watermark, indexed_watermark)
        valid = f"SELECT message_id FROM {_ROWS} WHERE indexed=1 AND message_id NOT IN (SELECT message_id FROM {_DIRTY}) UNION ALL SELECT message_id FROM temp.{_VALID}"
        pending = f"SELECT message_id FROM {_ROWS} WHERE indexed=0 AND message_id NOT IN (SELECT message_id FROM {_DIRTY}) UNION ALL SELECT message_id FROM temp.{_PENDING}"
        return SearchState(status, valid, pending)
    except (sqlite3.Error, MemoryFailure) as error:
        check()
        if not isinstance(error, MemoryFailure) or error.code == "lexical_checkpoint_conflict":
            if not db.in_transaction:
                raise
            if saved:
                db.execute("ROLLBACK TO evernook_checkpoint_query")
                db.execute("RELEASE evernook_checkpoint_query")
            return None
        raise
