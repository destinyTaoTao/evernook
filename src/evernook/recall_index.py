"""Optional SQLite-maintained indexes for exact candidate retrieval and coverage."""
from __future__ import annotations

import sqlite3

from .models import MemoryFailure


# Keep the index predicate and query predicate identical so SQLite can prove
# that every qualifying row is present. Visibility and generated evidence are
# checked against their own authoritative tables in the same read snapshot.
ORIGINAL = "json_extract(messages.receipt,'$.original_saved')=1 AND json_extract(messages.event,'$.text')<>''"
BY_SOURCE = "evernook_recall_source_v1"
BY_TIME = "evernook_recall_time_v1"
BY_CHUNK_VERSION = "evernook_recall_chunk_version_v1"
_DEFINITIONS = {
    BY_SOURCE: f"CREATE INDEX {BY_SOURCE} ON messages(id,json_extract(receipt,'$.content_version'),occurred,received) WHERE {ORIGINAL}",
    BY_TIME: f"CREATE INDEX {BY_TIME} ON messages(occurred DESC,id) WHERE {ORIGINAL}",
    # Match the chunk primary-key order so a competing vector scan plan does
    # not change which equal-distance span is visited first.
    BY_CHUNK_VERSION: f"CREATE INDEX {BY_CHUNK_VERSION} ON semantic_chunks_v1(space_id,message_id,start,state_version,content_version)",
}


def _definitions(db: sqlite3.Connection) -> dict[str, str | None]:
    definitions: dict[str, str | None] = {}
    for name, sql in db.execute("""SELECT lower(name),sql FROM sqlite_master
            WHERE name COLLATE NOCASE IN (?,?,?)""", tuple(_DEFINITIONS)):
        # SQLite gives triggers a separate namespace. Every matching object
        # must be owned before maintenance can change either index.
        definitions[name] = None if name in definitions else sql
    return definitions


def enabled(db: sqlite3.Connection) -> bool:
    existing = _definitions(db)
    return all(existing.get(name) == _DEFINITIONS[name] for name in (BY_SOURCE, BY_TIME))


def source_table(db: sqlite3.Connection, *, chronological: bool = False) -> str:
    if not enabled(db):
        return "messages"
    return "messages INDEXED BY " + (BY_TIME if chronological else BY_SOURCE)


def configure(db: sqlite3.Connection, *, enabled: bool, rebuild: bool) -> None:
    # All DDL runs in the caller's single write transaction. Refuse to remove
    # an object with the same name but a definition this version does not own.
    existing = _definitions(db)
    if any(sql != _DEFINITIONS[name] for name, sql in existing.items()):
        raise MemoryFailure("recall_candidate_index_conflict")
    desired = _DEFINITIONS.copy() if enabled else {}
    if enabled and db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_chunks_v1' AND type='table'").fetchone() is None:
        del desired[BY_CHUNK_VERSION]
    if existing == desired and not rebuild:
        return
    from . import lexical_checkpoint
    checkpoint_valid = lexical_checkpoint.enabled(db)
    for name, sql in _DEFINITIONS.items():
        if name in existing and (name not in desired or rebuild):
            db.execute(f"DROP INDEX {name}")
        if name in desired and (name not in existing or rebuild):
            db.execute(sql)
    if checkpoint_valid:
        # This transaction changed only the known indexes, leaving all
        # checkpoint sources, tracking triggers and pending rows untouched.
        db.execute(f"""UPDATE {lexical_checkpoint._HEAD}
            SET schema_version=(SELECT schema_version FROM pragma_schema_version) WHERE singleton=1""")
