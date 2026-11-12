"""Exact owned SQL for optional topic freshness; exports never carry executable triggers."""
from __future__ import annotations

import sqlite3

from .models import MemoryFailure


def definitions() -> dict[str, tuple[str, str]]:
    result = {}
    def eligible(reference: str) -> str:
        return (f"(json_extract({reference}.event,'$.role') IN ('user','assistant','tool')"
            f" AND json_extract({reference}.event,'$.completion')='complete'"
            f" AND json_extract({reference}.receipt,'$.original_saved')=1"
            f" AND json_extract({reference}.receipt,'$.representation')='original'"
            f" AND coalesce(json_extract({reference}.event,'$.sensitive'),0)=0"
            f" AND coalesce(json_extract({reference}.event,'$.protected_source'),0)=0)")
    for table, actions in (('messages', (('INSERT', 'evidence'), ('DELETE', 'control'), ('UPDATE', 'control'))),
            ('visibility', (('INSERT', 'control'), ('UPDATE', 'control'), ('DELETE', 'control'))),
            ('observation_events_v1', (('INSERT', 'derived'), ('UPDATE', 'derived'), ('DELETE', 'derived')))):
        for action, counter in actions:
            name = 'topic_clock_' + table + '_' + action.lower() + '_v1'
            last = ',last_source_id=NEW.id' if table == 'messages' and action == 'INSERT' else ''
            when = ''
            if table == 'messages':
                condition = eligible('NEW') if action == 'INSERT' else eligible('OLD') if action == 'DELETE' else eligible('OLD') + ' OR ' + eligible('NEW')
                when = ' WHEN ' + condition
            result[name] = (table, f'CREATE TRIGGER {name} AFTER {action} ON {table}{when} BEGIN UPDATE topic_view_clock_v1 SET {counter}={counter}+1{last} WHERE id=1; END')
    return result


def _owned(db: sqlite3.Connection) -> set[str]:
    expected = definitions()
    found = set()
    for row in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master WHERE lower(name) GLOB 'topic_clock_*'"):
        if row[0] not in expected or row[1] != 'trigger' or (row[2], row[3]) != expected[row[0]]:
            raise MemoryFailure('topic_view_clock_schema')
        found.add(str(row[0]))
    return found


def install(db: sqlite3.Connection) -> None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_clock_v1'").fetchone():
        return
    found = _owned(db)
    for name, (table, sql) in definitions().items():
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            raise MemoryFailure('topic_view_clock_schema')
        if name not in found:
            db.execute(sql)


def ready(db: sqlite3.Connection) -> bool:
    return _owned(db) == set(definitions())


def strip_export(db: sqlite3.Connection) -> None:
    found = _owned(db)
    if found and not db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_view_clock_v1'").fetchone():
        raise MemoryFailure('archive_integrity_mismatch')
    for name in found:
        db.execute(f'DROP TRIGGER {name}')
