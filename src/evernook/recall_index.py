"""Optional SQLite-maintained source projections for exact candidate retrieval."""
from __future__ import annotations

import sqlite3

from .models import MemoryFailure


# Keep the index predicate and query predicate identical so SQLite can prove
# that every qualifying row is present. Visibility and generated evidence are
# checked against their own authoritative tables in the same read snapshot.
ORIGINAL = "json_extract(messages.receipt,'$.original_saved')=1 AND json_extract(messages.event,'$.text')<>''"
BY_SOURCE = "evernook_recall_source_v1"
BY_TIME = "evernook_recall_time_v1"
_DEFINITIONS = {
    BY_SOURCE: f"CREATE INDEX {BY_SOURCE} ON messages(id,json_extract(receipt,'$.content_version'),occurred,received) WHERE {ORIGINAL}",
    BY_TIME: f"CREATE INDEX {BY_TIME} ON messages(occurred DESC,id) WHERE {ORIGINAL}",
}


def _definitions(db: sqlite3.Connection) -> dict[str, str | None]:
    definitions: dict[str, str | None] = {}
    for name, sql in db.execute("""SELECT lower(name),sql FROM sqlite_master
            WHERE name COLLATE NOCASE IN (?,?)""", (BY_SOURCE, BY_TIME)):
        # SQLite gives triggers a separate namespace. Every matching object
        # must be owned before maintenance can change either index.
        definitions[name] = None if name in definitions else sql
    return definitions


def enabled(db: sqlite3.Connection) -> bool:
    return _definitions(db) == _DEFINITIONS


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
    if (enabled and existing == _DEFINITIONS and not rebuild) or (not enabled and not existing):
        return
    from . import lexical_checkpoint
    checkpoint_valid = lexical_checkpoint.enabled(db)
    for name, sql in _DEFINITIONS.items():
        if name in existing and (not enabled or rebuild):
            db.execute(f"DROP INDEX {name}")
        if enabled and (name not in existing or rebuild):
            db.execute(sql)
    if checkpoint_valid:
        # This transaction changed only the two known B-trees, leaving all
        # checkpoint sources, tracking triggers and pending rows untouched.
        db.execute(f"""UPDATE {lexical_checkpoint._HEAD}
            SET schema_version=(SELECT schema_version FROM pragma_schema_version) WHERE singleton=1""")
