"""Opt-in conservative int8 cosine screening. All returned scores still come from the source float32 chunks."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
import hashlib
import json
import math
import platform
import sqlite3
import struct
import sys

from . import lexical_checkpoint, semantic_checkpoint
from .models import MemoryFailure

_PREFIX = "evernook_vector_screen_v1_"
_ROWS, _HEAD, _DIRTY = (_PREFIX + suffix for suffix in ("rows", "head", "dirty"))
_MAX_DIRTY = 100
_MAX_CANDIDATES = 4096


def _definitions() -> dict[str, tuple[str, str]]:
    definitions = {
        _ROWS: ("table", f"CREATE TABLE {_ROWS}(message_id TEXT NOT NULL,start INTEGER NOT NULL,"
                         "code BLOB,radius REAL NOT NULL,PRIMARY KEY(message_id,start)) WITHOUT ROWID"),
        _HEAD: ("table", f"CREATE TABLE {_HEAD}(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                         "space TEXT NOT NULL,dimensions INTEGER NOT NULL,seal TEXT NOT NULL)"),
        _DIRTY: ("table", f"CREATE TABLE {_DIRTY}(message_id TEXT PRIMARY KEY) WITHOUT ROWID"),
    }
    for action, refs in (("insert", ("NEW",)), ("update", ("OLD", "NEW")), ("delete", ("OLD",))):
        name = _PREFIX + action
        body = "".join(
            f"INSERT INTO {_DIRTY} SELECT {ref}.message_id WHERE {ref}.space_id=(SELECT space FROM {_HEAD}) "
            f"AND NOT EXISTS (SELECT 1 FROM {_DIRTY} WHERE message_id={ref}.message_id);" for ref in refs)
        definitions[name] = ("trigger", f"CREATE TRIGGER {name} AFTER {action.upper()} ON semantic_chunks_v1 BEGIN {body} END")
    for table, actions in ((_ROWS, ("insert", "delete", "update")), (_DIRTY, ("delete", "update"))):
        for action in actions:
            name = table + "_guard_" + action
            definitions[name] = ("trigger", f"CREATE TRIGGER {name} AFTER {action.upper()} ON {table} "
                                            f"BEGIN UPDATE {_HEAD} SET seal='' WHERE seal<>''; END")
    return definitions


_DEFINITIONS = _definitions()


def _owned(db: sqlite3.Connection) -> set[str]:
    found: set[str] = set()
    for name, kind, sql in db.execute("SELECT name,type,sql FROM sqlite_schema WHERE lower(name) GLOB ?", (_PREFIX + "*",)):
        if name in found or _DEFINITIONS.get(name) != (kind, sql):
            raise MemoryFailure("vector_screen_conflict")
        found.add(name)
    if found and any(name not in found for (name,) in db.execute(
            "SELECT name FROM sqlite_schema WHERE tbl_name IN (?,?,?) AND sql IS NOT NULL", (_ROWS, _DIRTY, _HEAD))):
        raise MemoryFailure("vector_screen_conflict")
    return found


def _seal(db: sqlite3.Connection, space: str, dimensions: int) -> str:
    schema = db.execute("PRAGMA main.schema_version").fetchone()[0]
    return hashlib.sha256(repr(("int8-angular-bound-v1", space, dimensions, schema)).encode()).hexdigest()


def _head(db: sqlite3.Connection, found: set[str]) -> tuple[str, int] | None:
    if found != _DEFINITIONS.keys():
        return None
    row = db.execute(f"SELECT space,dimensions,seal FROM {_HEAD} WHERE singleton=1").fetchone()
    if row is None or type(row[1]) is not int or not 1 <= row[1] <= 4096 or row[2] != _seal(db, row[0], row[1]):
        return None
    return row[0], row[1]


def encode(raw: bytes, dimensions: int) -> tuple[bytes | None, float]:
    if not isinstance(raw, bytes) or len(raw) != dimensions * 4:
        return None, 2.0
    values = struct.unpack(f"<{dimensions}f", raw)
    norm = math.hypot(*values)
    # Other representations still enter authoritative scoring. This range also
    # keeps all native float32 sums of squares well away from under/overflow.
    if not .75 <= norm <= 1.25:
        return None, 2.0
    peak = max(map(abs, values))
    scale = 127 / peak
    code = bytes(round(x * scale) & 255 for x in values)
    # Rounding to nearest perturbs each scaled coordinate by at most half a
    # step. Normalizing the rounded vector costs at most twice that L2 error.
    radius = math.sqrt(dimensions) * peak / (127 * norm) + 1e-12
    return code, radius


def _encoded_rows(rows: Iterable[sqlite3.Row], dimensions: int,
                  check: Callable[[], float] | None) -> Iterator[tuple[str, int, bytes | None, float]]:
    for index, row in enumerate(rows):
        if check is not None and index % 128 == 0:
            check()
        code, radius = encode(row[2], dimensions)
        yield row[0], row[1], code, radius


def _drop(db: sqlite3.Connection, found: set[str]) -> None:
    if found:
        # A dropped compact representation must not survive in free archive
        # pages. Purge enables secure_delete for its own transaction as well.
        db.execute("PRAGMA secure_delete=ON")
    for kind in ("trigger", "table"):
        for name in sorted(found):
            if _DEFINITIONS[name][0] == kind:
                db.execute(f"DROP {kind} {name}")


def strip_export(db: sqlite3.Connection) -> None:
    try:
        _drop(db, _owned(db))
    except MemoryFailure as error:
        raise MemoryFailure("archive_integrity_mismatch") from error


def configure(db: sqlite3.Connection, space: str | None, dimensions: int | None, *, enabled: bool, rebuild: bool,
              check: Callable[[], float] | None) -> None:
    found = _owned(db)
    if enabled:
        if not space or dimensions is None:
            raise MemoryFailure("embedding_disabled")
        if not rebuild:
            head = _head(db, found)
            if head == (space, dimensions):
                return
            if head is not None:
                raise MemoryFailure("vector_screen_space_mismatch")
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE name='semantic_chunks_v1' AND type='table'").fetchone():
            raise MemoryFailure("vector_screen_requires_index")
    elif not found:
        return
    lexical_valid = lexical_checkpoint.enabled(db)
    try:
        semantic_head = semantic_checkpoint._head(db, semantic_checkpoint._owned(db))
    except MemoryFailure as failure:
        if failure.code != "semantic_checkpoint_conflict":
            raise
        semantic_head = None
    _drop(db, found)
    if enabled:
        assert space is not None and dimensions is not None
        for kind, sql in _DEFINITIONS.values():
            if kind == "table":
                db.execute(sql)
        select_rows = "SELECT message_id,start,vector FROM semantic_chunks_v1 WHERE space_id=? ORDER BY message_id,start"
        with closing(db.execute(select_rows, (space,))) as rows:
            db.executemany(f"INSERT INTO {_ROWS} VALUES(?,?,?,?)", _encoded_rows(rows, dimensions, check))
        for kind, sql in _DEFINITIONS.values():
            if kind == "trigger":
                db.execute(sql)
        db.execute(f"INSERT INTO {_HEAD} VALUES(1,?,?,?)", (space, dimensions, _seal(db, space, dimensions)))
    if lexical_valid:
        db.execute(f"UPDATE {lexical_checkpoint._HEAD} SET schema_version="
                   "(SELECT schema_version FROM pragma_schema_version) WHERE singleton=1")
    if semantic_head is not None:
        db.execute(f"UPDATE {semantic_checkpoint._HEAD} SET seal=? WHERE singleton=1", (semantic_head.seal(db),))


def _dirty(db: sqlite3.Connection, limit: int) -> list[str]:
    return [r[0] for r in db.execute(f"SELECT message_id FROM {_DIRTY} LIMIT ?", (limit,))]


def refresh(db: sqlite3.Connection, space: str | None, limit: int, check: Callable[[], float] | None) -> int:
    head = _head(db, _owned(db))
    if head is None:
        raise MemoryFailure("vector_screen_invalid")
    if space is not None and head[0] != space:
        raise MemoryFailure("vector_screen_space_mismatch")
    ids = _dirty(db, limit)
    if ids:
        db.execute("PRAGMA secure_delete=ON")
    for message_id in ids:
        if check is not None:
            check()
        db.execute(f"DELETE FROM {_ROWS} WHERE message_id=?", (message_id,))
        with closing(db.execute("SELECT message_id,start,vector FROM semantic_chunks_v1 WHERE space_id=? AND message_id=?",
                                (head[0], message_id))) as rows:
            db.executemany(f"INSERT INTO {_ROWS} VALUES(?,?,?,?)", _encoded_rows(rows, head[1], check))
        db.execute(f"DELETE FROM {_DIRTY} WHERE message_id=?", (message_id,))
    if ids:
        db.execute(f"UPDATE {_HEAD} SET seal=? WHERE singleton=1", (_seal(db, *head),))
    return len(ids)


def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    found = _owned(db)
    head = _head(db, found)
    for table in (_ROWS, _DIRTY):
        if table in found:
            db.executemany(f"DELETE FROM {table} WHERE message_id=?", ((i,) for i in ids))
    if head is not None:
        db.execute(f"UPDATE {_HEAD} SET seal=? WHERE singleton=1", (_seal(db, *head),))


def candidates(db: sqlite3.Connection, space: str, encoded: bytes, minimum: float) -> list[str] | None:
    # Audited native kernel and IEEE binary32 arithmetic are specific to
    # the pinned Windows x64 backend. Other platforms keep exact scanning.
    if minimum <= 0 or sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        return None
    try:
        dimensions = len(encoded) // 4
        if _head(db, _owned(db)) != (space, dimensions):
            return None
        dirty = _dirty(db, _MAX_DIRTY + 1)
        if len(dirty) > _MAX_DIRTY:
            return None
        if dirty:
            # Keep malformed-vector error behavior on the full canonical plan.
            # A long changed source can be compacted offline before using it.
            with closing(db.execute("""SELECT vector FROM semantic_chunks_v1 WHERE space_id=? AND
                    message_id IN (SELECT value FROM json_each(?)) LIMIT ?""",
                    (space, json.dumps(dirty), _MAX_DIRTY + 1))) as changed:
                for index, row in enumerate(changed):
                    if index == _MAX_DIRTY or encode(row[0], dimensions)[0] is None:
                        return None
        query, radius = encode(encoded, dimensions)
        if query is None:
            return None
        # Unit-vector dot changes by at most the sum of both L2 radii.
        # Cover both native float32 cosines (dot, squared norms, distance
        # rounding) and binary64 additions. See the validation report.
        margin = radius + 64 * dimensions * 2 ** -24
        rows = list(db.execute(f"""SELECT message_id,code IS NULL FROM {_ROWS} WHERE code IS NULL OR
            coalesce(1.0-vec_distance_cosine(vec_int8(code),vec_int8(?)),1.0)+radius+?>=?
            LIMIT ?""", (query, margin, minimum, _MAX_CANDIDATES + 1)))
        if len(rows) > _MAX_CANDIDATES or any(row[1] for row in rows):
            return None
        return sorted({r[0] for r in rows}.union(dirty))
    except MemoryFailure as failure:
        if failure.code != "vector_screen_conflict":
            raise
        return None
    except sqlite3.Error as failure:
        if not db.in_transaction or (getattr(failure, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT):
            raise
        return None
