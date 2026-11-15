"""Authenticated destruction tombstones; original links remain evidence."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3

from .models import MemoryFailure
from .runtime import MemoryRuntime, _json


def _auth(runtime: MemoryRuntime, inbox_id: str, key_id: str) -> str:
    return hmac.digest(runtime._fingerprint_key, b'evernook-vault-retirement-v1\0'
                       + _json([inbox_id, key_id]).encode(), hashlib.sha256).hex()


def retired(runtime: MemoryRuntime, core: sqlite3.Connection) -> dict[str, str]:
    if not core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_retirements_v1'").fetchone():
        return {}
    result: dict[str, str] = {}
    try:
        for row in core.execute('SELECT inbox_id,key_id,authentication FROM vault_retirements_v1'):
            inbox_id, key_id, authentication = row
            if (not isinstance(inbox_id, str) or not isinstance(key_id, str)
                    or not isinstance(authentication, str)
                    or not hmac.compare_digest(authentication, _auth(runtime, inbox_id, key_id))):
                raise ValueError()
            result[inbox_id] = key_id
    except (sqlite3.Error, ValueError, TypeError):
        raise MemoryFailure('vault_retirement_invalid') from None
    return result


def record(runtime: MemoryRuntime, core: sqlite3.Connection, inbox_id: str, key_id: str) -> None:
    core.execute('CREATE TABLE IF NOT EXISTS vault_retirements_v1 '
                 '(inbox_id TEXT PRIMARY KEY, key_id TEXT NOT NULL, authentication TEXT NOT NULL)')
    existing = retired(runtime, core)
    if inbox_id in existing and existing[inbox_id] != key_id:
        raise MemoryFailure('vault_retirement_invalid')
    core.execute('INSERT OR IGNORE INTO vault_retirements_v1 VALUES (?,?,?)',
                 (inbox_id, key_id, _auth(runtime, inbox_id, key_id)))


def active_registry(runtime: MemoryRuntime, core: sqlite3.Connection) -> dict[str, str]:
    gone = retired(runtime, core)
    if not core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_inbox_registry_v1'").fetchone():
        return {}
    return {str(row[0]): str(row[1]) for row in core.execute('SELECT inbox_id,key_id FROM protected_inbox_registry_v1')
            if gone.get(str(row[0])) != str(row[1])}


def active_link_count(runtime: MemoryRuntime, core: sqlite3.Connection) -> int:
    gone = retired(runtime, core)
    if not core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_links_v1'").fetchone():
        return 0
    if not gone:
        return int(core.execute('SELECT count(*) FROM protected_links_v1').fetchone()[0])
    # No bound on historical tombstones is used to truncate evidence. The
    # original references stay in place and remain available for auditing.
    try:
        return int(core.execute('''SELECT count(*) FROM protected_links_v1 l WHERE NOT EXISTS
            (SELECT 1 FROM vault_retirements_v1 r
             WHERE r.inbox_id=json_extract(l.reference,'$.inbox_id')
             AND r.key_id=json_extract(l.reference,'$.key_id'))''').fetchone()[0])
    except sqlite3.Error:
        raise MemoryFailure('protected_link_unavailable') from None
