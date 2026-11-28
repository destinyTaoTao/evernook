"""Optional per-space semantic coverage for one SQLite snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from functools import cache
import hashlib
import sqlite3

from .models import MemoryFailure


_PREFIX = "evernook_semantic_checkpoint_v1_"
_ROWS = _PREFIX + "rows"
_HEAD = _PREFIX + "head"
_DIRTY = _PREFIX + "dirty"
_PENDING_INDEX = _PREFIX + "pending_received"
_READ_LIMIT = 100
_MAINTENANCE_LIMIT = 5000
_SOURCES = ("messages", "visibility", "captures", "semantic_documents_v1",
            "semantic_chunks_v1", "semantic_failures_v1")
_VECTOR_ONLY = f"""SELECT c.message_id,NULL,0,0,count(*) FROM semantic_chunks_v1 c
    WHERE c.space_id=:space AND NOT EXISTS (SELECT 1 FROM {_ROWS} r WHERE r.message_id=c.message_id)
    GROUP BY c.message_id"""


def _definitions() -> dict[str, tuple[str, str]]:
    result = {
        _ROWS: ("table", f"CREATE TABLE {_ROWS}(message_id TEXT PRIMARY KEY, received TEXT, covered INTEGER NOT NULL, failed INTEGER NOT NULL, vectors INTEGER NOT NULL) WITHOUT ROWID"),
        _HEAD: ("table", f"CREATE TABLE {_HEAD}(singleton INTEGER PRIMARY KEY CHECK(singleton=1), space TEXT NOT NULL, sources INTEGER NOT NULL, covered INTEGER NOT NULL, failed INTEGER NOT NULL, vectors INTEGER NOT NULL, seal TEXT NOT NULL)"),
        _DIRTY: ("table", f"CREATE TABLE {_DIRTY}(message_id TEXT PRIMARY KEY) WITHOUT ROWID"),
        _PREFIX + "received": ("index", f"CREATE INDEX {_PREFIX}received ON {_ROWS}(received)"),
        _PENDING_INDEX: ("index", f"CREATE INDEX {_PENDING_INDEX} ON {_ROWS}(received,message_id) WHERE covered=0 AND received IS NOT NULL"),
    }
    for table in _SOURCES:
        key = "id" if table == "messages" else "message_id"
        for action, refs in (("insert", ("NEW",)), ("delete", ("OLD",)), ("update", ("OLD", "NEW"))):
            name = _PREFIX + table + "_" + action
            body = ""
            for ref in refs:
                # WHERE NOT EXISTS also works inside an outer INSERT OR REPLACE.
                condition = f"{ref}.{key} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {_DIRTY} WHERE message_id={ref}.{key})"
                if table.startswith("semantic_"):
                    condition += f" AND {ref}.space_id=(SELECT space FROM {_HEAD} WHERE singleton=1)"
                body += f"INSERT INTO {_DIRTY} SELECT {ref}.{key} WHERE {condition};"
            result[name] = ("trigger", f"CREATE TRIGGER {name} AFTER {action.upper()} ON {table} BEGIN {body} END")
    # SQLite REPLACE can silently delete a different id on messages.source's
    # UNIQUE constraint. Its implicit deletion need not run delete triggers.
    for action in ("insert", "update"):
        name = _PREFIX + "source_collision_" + action
        result[name] = ("trigger", f"""CREATE TRIGGER {name} BEFORE {action.upper()} ON messages BEGIN
            INSERT INTO {_DIRTY} SELECT id FROM messages WHERE source=NEW.source AND id<>NEW.id
            AND id NOT IN (SELECT message_id FROM {_DIRTY}); END""")
    # Only trusted maintenance reseals the head, in the same write transaction.
    # Unexpected edits to derived membership or lost dirty entries force a scan.
    for table, actions in ((_ROWS, ("insert", "delete", "update")), (_DIRTY, ("update", "delete"))):
        for action in actions:
            name = table + "_guard_" + action
            result[name] = ("trigger", f"CREATE TRIGGER {name} AFTER {action.upper()} ON {table} BEGIN UPDATE {_HEAD} SET seal='' WHERE singleton=1 AND seal<>''; END")
    return result


_DEFINITIONS = _definitions()
_LEGACY = _DEFINITIONS.keys() - {_PENDING_INDEX}


def _owned(db: sqlite3.Connection) -> set[str]:
    found: set[str] = set()
    for name, kind, sql in db.execute("SELECT name,type,sql FROM sqlite_schema WHERE lower(name) GLOB ?", (_PREFIX + "*",)):
        if name in found or _DEFINITIONS.get(name) != (kind, sql):
            raise MemoryFailure("semantic_checkpoint_conflict")
        found.add(name)
    if found:
        attached = db.execute("SELECT name FROM sqlite_schema WHERE tbl_name IN (?,?,?) AND sql IS NOT NULL",
                              (_HEAD, _ROWS, _DIRTY))
        if any(name not in found for (name,) in attached):
            raise MemoryFailure("semantic_checkpoint_conflict")
    return found


@cache
def _projection(single: bool) -> str:
    from .semantic import SemanticRecall, _JOINED_ELIGIBLE
    covered = SemanticRecall._covered(space_parameter=":space")
    if single:
        covered += " AND d.message_id=:id"
    return f"""SELECT messages.id,messages.received,messages.id IN ({covered}),
        (SELECT count(*) FROM semantic_failures_v1 f WHERE f.space_id=:space AND f.message_id=messages.id
         AND f.state_version=coalesce(v.version,1) AND f.content_version=json_extract(messages.receipt,'$.content_version')),
        (SELECT count(*) FROM semantic_chunks_v1 c WHERE c.space_id=:space AND c.message_id=messages.id)
        FROM messages LEFT JOIN visibility v ON v.message_id=messages.id
        WHERE {_JOINED_ELIGIBLE} {"AND messages.id=:id" if single else ""}"""


@dataclass(frozen=True)
class _Head:
    space: str
    sources: int
    covered: int
    failed: int
    vectors: int

    def seal(self, db: sqlite3.Connection) -> str:
        schema = db.execute("PRAGMA main.schema_version").fetchone()[0]
        return hashlib.sha256(repr((schema, _projection(False), _projection(True), _VECTOR_ONLY, self)).encode()).hexdigest()


def _head(db: sqlite3.Connection, found: set[str]) -> _Head | None:
    if not _LEGACY <= found:
        return None
    row = db.execute(f"SELECT space,sources,covered,failed,vectors,seal FROM {_HEAD} WHERE singleton=1").fetchone()
    if row is None or not row[5] or any(type(n) is not int for n in row[1:5]):
        return None
    head = _Head(*row[:5])
    if not 0 <= head.covered <= head.sources or min(head.failed, head.vectors) < 0 or head.seal(db) != row[5]:
        return None
    return head


def _drop(db: sqlite3.Connection, found: set[str]) -> None:
    for kind in ("trigger", "index", "table"):
        for name in found:
            if _DEFINITIONS[name][0] == kind:
                db.execute(f"DROP {kind} {name}")


def strip_export(db: sqlite3.Connection) -> None:
    try:
        found = _owned(db)
    except MemoryFailure as error:
        raise MemoryFailure("archive_integrity_mismatch") from error
    _drop(db, found)


def configure(db: sqlite3.Connection, space: str | None, *, enabled: bool, rebuild: bool) -> None:
    from . import lexical_checkpoint
    found = _owned(db)
    head = _head(db, found)
    if enabled:
        if space is None:
            raise MemoryFailure("embedding_disabled")
        if head and head.space == space and not rebuild:
            return
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if not {*_SOURCES, "semantic_spaces_v1"} <= tables:
            raise MemoryFailure("semantic_checkpoint_requires_index")
    elif not found:
        return
    lexical_valid = lexical_checkpoint.enabled(db)
    _drop(db, found)
    if enabled:
        assert space is not None
        for kind, sql in _DEFINITIONS.values():
            if kind == "table":
                db.execute(sql)
        db.execute(f"INSERT INTO {_ROWS} " + _projection(False) + " ORDER BY messages.id", {"space": space})
        # Stored vectors include stale/ineligible sources, even an orphan in a
        # historical database written without foreign-key enforcement.
        db.execute(f"INSERT INTO {_ROWS} " + _VECTOR_ONLY, {"space": space})
        for kind, sql in _DEFINITIONS.values():
            if kind != "table":
                db.execute(sql)
        counts = db.execute(f"SELECT count(received),coalesce(sum(covered),0),coalesce(sum(failed),0),coalesce(sum(vectors),0) FROM {_ROWS}").fetchone()
        head = _Head(space, *counts)
        db.execute(f"INSERT INTO {_HEAD} VALUES(1,?,?,?,?,?,?)", (space, *counts, head.seal(db)))
    if lexical_valid:
        db.execute(f"UPDATE {lexical_checkpoint._HEAD} SET schema_version=(SELECT schema_version FROM pragma_schema_version) WHERE singleton=1")


def _dirty(db: sqlite3.Connection, limit: int) -> list[str]:
    return [r[0] for r in db.execute(f"SELECT message_id FROM {_DIRTY} ORDER BY message_id LIMIT ?", (limit,))]


def _reconcile(db: sqlite3.Connection, head: _Head, ids: list[str] | tuple[str, ...], *,
               persist: bool, completed_only: bool = False) -> tuple[_Head, str | None]:
    sources, covered, failed, vectors = head.sources, head.covered, head.failed, head.vectors
    newest = None
    for message_id in ids:
        current = db.execute(_projection(True), {"space": head.space, "id": message_id}).fetchone()
        if completed_only and current is not None and not current[2]:
            continue  # Keep a partially embedded document in the dirty work set.
        old = db.execute(f"SELECT received,covered,failed,vectors FROM {_ROWS} WHERE message_id=?", (message_id,)).fetchone()
        if current is None:
            current = db.execute("""SELECT message_id,NULL,0,0,count(*) FROM semantic_chunks_v1
                WHERE space_id=? AND message_id=? GROUP BY message_id""", (head.space, message_id)).fetchone()
        if old is not None:
            sources -= int(old[0] is not None)
            covered -= old[1]
            failed -= old[2]
            vectors -= old[3]
        if current is not None:
            if current[1] is not None:
                sources += 1
                newest = max(newest, current[1]) if newest is not None else current[1]
            covered += current[2]
            failed += current[3]
            vectors += current[4]
        if persist:
            db.execute(f"DELETE FROM {_ROWS} WHERE message_id=?", (message_id,))
            if current is not None:
                db.execute(f"INSERT INTO {_ROWS} VALUES(?,?,?,?,?)", tuple(current))
            db.execute(f"DELETE FROM {_DIRTY} WHERE message_id=?", (message_id,))
    updated = _Head(head.space, sources, covered, failed, vectors)
    if persist and ids:
        db.execute(f"UPDATE {_HEAD} SET sources=?,covered=?,failed=?,vectors=?,seal=? WHERE singleton=1",
                   (sources, covered, failed, vectors, updated.seal(db)))
    return updated, newest


def _optional_read[T](db: sqlite3.Connection, read: Callable[[], T]) -> T | None:
    try:
        return read()
    except MemoryFailure as failure:
        if failure.code != "semantic_checkpoint_conflict":
            raise
        return None
    except sqlite3.Error as failure:
        # Read the authoritative rows in the same snapshot if derived pages
        # cannot be read. Preserve busy, I/O, interrupt and lost-snapshot errors.
        if (not db.in_transaction or
                (getattr(failure, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CORRUPT)):
            raise
        return None


def _space_head(db: sqlite3.Connection, space: str) -> _Head | None:
    head = _head(db, _owned(db))
    return head if head is not None and head.space == space else None


def pending(db: sqlite3.Connection, space: str, limit: int) -> list[sqlite3.Row] | None:
    from .semantic import SemanticRecall, _JOINED_ELIGIBLE

    def read() -> list[sqlite3.Row] | None:
        found = _owned(db)
        head = _head(db, found)
        if head is None or head.space != space:
            return None
        candidates = f"SELECT message_id FROM {_DIRTY}"
        if head.covered < head.sources:
            if _PENDING_INDEX not in found:
                return None
            # At most the first `limit` clean pending sources can win the
            # merged ordering. Dirty IDs (including changed clean rows) are
            # independently rechecked. Older complete heads need no new index.
            candidates += f""" UNION ALL SELECT message_id FROM (SELECT message_id FROM {_ROWS} INDEXED BY {_PENDING_INDEX}
                WHERE covered=0 AND received IS NOT NULL AND message_id NOT IN (SELECT message_id FROM {_DIRTY})
                ORDER BY received,message_id LIMIT :limit)"""
        covered = SemanticRecall._covered(space_parameter=":space")
        return db.execute(f"""WITH candidates AS MATERIALIZED ({candidates})
            SELECT messages.event,messages.receipt,coalesce(v.version,1) AS state_version
            FROM candidates q CROSS JOIN messages ON messages.id=q.message_id
            LEFT JOIN visibility v ON v.message_id=q.message_id
            WHERE {_JOINED_ELIGIBLE} AND NOT EXISTS ({covered} AND d.message_id=q.message_id)
            ORDER BY messages.received,messages.id LIMIT :limit""", {"space": space, "limit": limit}).fetchall()

    return _optional_read(db, read)


def acknowledge(db: sqlite3.Connection, space: str, ids: tuple[str, ...]) -> None:
    head = _optional_read(db, lambda: _space_head(db, space))
    if head is not None:
        _reconcile(db, head, ids, persist=True, completed_only=True)


def status(db: sqlite3.Connection, space: str, *, maintenance: bool = False) -> tuple[int, int, int, int, str | None] | None:
    limit = _MAINTENANCE_LIMIT if maintenance else _READ_LIMIT
    return _optional_read(db, lambda: _status(db, space, limit))


def _status(db: sqlite3.Connection, space: str, limit: int) -> tuple[int, int, int, int, str | None] | None:
    head = _space_head(db, space)
    if head is None:
        return None
    ids = _dirty(db, limit + 1)
    if len(ids) > limit:
        return None
    clean = db.execute(f"""SELECT received FROM {_ROWS} WHERE received IS NOT NULL AND message_id NOT IN (SELECT message_id FROM {_DIRTY})
        ORDER BY received DESC LIMIT 1""").fetchone()
    updated, newest = _reconcile(db, head, ids, persist=False)
    if clean is not None:
        newest = max(clean[0], newest) if newest is not None else clean[0]
    return updated.sources, updated.covered, updated.failed, updated.vectors, newest


def refresh(db: sqlite3.Connection, limit: int, *, space: str | None = None) -> int:
    head = _head(db, _owned(db))
    if head is None:
        raise MemoryFailure("semantic_checkpoint_invalid")
    if space is not None and head.space != space:
        raise MemoryFailure("semantic_checkpoint_space_mismatch")
    ids = _dirty(db, limit)
    _reconcile(db, head, ids, persist=True)
    return len(ids)


def repair_if_stale(db: sqlite3.Connection, space: str, *, check: Callable[[], object] | None = None) -> bool:
    """Revalidate an explicitly configured checkpoint against one authoritative snapshot.

    The caller provides a dedicated autocommit connection with its interrupt
    handler installed. Writers remain free while the reference projection and
    its differences are built in private temporary tables. A concurrent commit
    invalidates this repair attempt before any main database changes.
    """
    from . import lexical_checkpoint

    proof = 'temp.evernook_semantic_repair_rows'
    changed = 'temp.evernook_semantic_repair_changed'
    removed = 'temp.evernook_semantic_repair_removed'
    staged = False
    db.execute('BEGIN')
    try:
        found = _optional_read(db, lambda: _owned(db))
        if found is None or not _LEGACY <= found:
            return False
        head = _head(db, found)
        if head is not None and (head.space != space or head.covered == head.sources or _PENDING_INDEX in found):
            return False
        configured = db.execute(f'SELECT space,seal FROM {_HEAD} WHERE singleton=1').fetchone()
        if not configured or configured[0] != space or not configured[1]:
            return False
        version = db.execute('PRAGMA main.data_version').fetchone()[0]
        lexical_valid = _PENDING_INDEX not in found and lexical_checkpoint.enabled(db)
        db.execute(f'CREATE TABLE {proof}(message_id TEXT PRIMARY KEY,received TEXT,covered INTEGER,failed INTEGER,vectors INTEGER) WITHOUT ROWID')
        db.execute(f'INSERT INTO {proof} ' + _projection(False) + ' ORDER BY messages.id', {'space': space})
        db.execute(f'INSERT INTO {proof} ' + _VECTOR_ONLY.replace(_ROWS, proof), {'space': space})
        counts = db.execute(f'SELECT count(received),coalesce(sum(covered),0),coalesce(sum(failed),0),coalesce(sum(vectors),0) FROM {proof}').fetchone()
        db.execute(f'''CREATE TABLE {changed} AS SELECT p.* FROM {proof} p LEFT JOIN {_ROWS} r USING(message_id)
            WHERE r.message_id IS NULL OR p.received IS NOT r.received OR p.covered<>r.covered
                OR p.failed<>r.failed OR p.vectors<>r.vectors''')
        db.execute(f'''CREATE TABLE {removed} AS SELECT r.message_id FROM {_ROWS} r
            WHERE NOT EXISTS (SELECT 1 FROM {proof} p WHERE p.message_id=r.message_id)''')
        db.execute('COMMIT')
        staged = True

        if check is not None:
            check()
        db.execute('BEGIN IMMEDIATE')
        if db.execute('PRAGMA main.data_version').fetchone()[0] != version:
            return False
        db.execute(f'DELETE FROM {_ROWS} WHERE message_id IN (SELECT message_id FROM {removed})')
        db.execute(f'INSERT OR REPLACE INTO {_ROWS} SELECT * FROM {changed}')
        db.execute(f'DELETE FROM {_DIRTY}')
        if _PENDING_INDEX not in found:
            db.execute(_DEFINITIONS[_PENDING_INDEX][1])
        proven = _Head(space, *counts)
        db.execute(f'UPDATE {_HEAD} SET sources=?,covered=?,failed=?,vectors=?,seal=? WHERE singleton=1',
                   (*counts, proven.seal(db)))
        if lexical_valid:
            db.execute(f'UPDATE {lexical_checkpoint._HEAD} SET schema_version=(SELECT schema_version FROM pragma_schema_version) WHERE singleton=1')
        if check is not None:
            check()
        db.execute('COMMIT')
        return True
    finally:
        if db.in_transaction:
            db.rollback()
        if staged:
            for table in (removed, changed, proof):
                db.execute(f'DROP TABLE {table}')


def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    found = _owned(db)
    head = _head(db, found)
    if head is not None:
        _reconcile(db, head, ids, persist=True)
    else:
        for table in (_ROWS, _DIRTY):
            if table in found:
                db.executemany(f"DELETE FROM {table} WHERE message_id=?", ((i,) for i in ids))
