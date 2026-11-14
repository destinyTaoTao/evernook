"""Fail-closed missing-vault state for an explicitly reviewed offline recovery."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any

from .models import MemoryFailure
from .paths import verify_private_directory
from .runtime import MemoryRuntime, _json, _load, _unique_object


def _signed_receipt(runtime: MemoryRuntime) -> tuple[dict[str, Any], str]:
    path = runtime.root / 'recovery-receipt.json'
    try:
        verify_private_directory(runtime.root, extra_files=(path,))
        content: dict[str, Any] = _load(path)
        authentication = content.pop('authentication', None)
        fields = {'version', 'backup_id', 'created_at', 'backup_fingerprint', 'archive_sha256', 'destination',
                  'later_changes', 'protected_references', 'vault_restored', 'maintenance_at_recovery',
                  'automatic_preference_admission_at_recovery', 'owner_id', 'access_sha256'}
        expected = hmac.digest(runtime._fingerprint_key, b'evernook-offline-recovery-v1\0'
                               + _json(content).encode(), hashlib.sha256).hex()
        if (set(content) != fields or type(content['version']) is not int or content['version'] != 1
                or content['destination'] != str(runtime.root) or content['later_changes'] != 'unknown'
                or type(content['protected_references']) is not bool or content['vault_restored'] is not False
                or not isinstance(authentication, str) or not hmac.compare_digest(authentication, expected)):
            raise MemoryFailure('protected_recovery_proof_invalid')
        return content, authentication
    except (OSError, TypeError, ValueError, KeyError, MemoryFailure):
        raise MemoryFailure('protected_recovery_proof_invalid') from None


def _receipt(runtime: MemoryRuntime) -> str:
    content, authentication = _signed_receipt(runtime)
    if content['protected_references'] is not True:
        raise MemoryFailure('protected_recovery_proof_invalid')
    return authentication


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check_projections_inner(db: sqlite3.Connection, *, require_protected: bool) -> None:
    has_links = _has_table(db, 'protected_links_v1')
    has_registry = _has_table(db, 'protected_inbox_registry_v1')
    if has_links:
        orphan = db.execute("""SELECT 1 FROM messages m LEFT JOIN protected_links_v1 l ON l.message_id=m.id
            WHERE json_extract(m.event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' AND l.message_id IS NULL LIMIT 1""").fetchone()
        links = db.execute('SELECT 1 FROM protected_links_v1 LIMIT 1').fetchone()
    else:
        orphan = db.execute("SELECT 1 FROM messages WHERE json_extract(event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' LIMIT 1").fetchone()
        links = None
    registry = db.execute('SELECT 1 FROM protected_inbox_registry_v1 LIMIT 1').fetchone() if has_registry else None
    if orphan or (require_protected and not (links or registry)):
        raise MemoryFailure('protected_recovery_proof_invalid')


def _check_projections(db: sqlite3.Connection, *, require_protected: bool) -> None:
    deadline = time.monotonic() + 30
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        _check_projections_inner(db, require_protected=require_protected)
    finally:
        db.set_progress_handler(None, 0)


def attachment(runtime: MemoryRuntime, db: sqlite3.Connection) -> dict[str, Any] | None:
    """Read an authenticated attachment; malformed or transitional state fails closed."""
    if not _has_table(db, 'restored_vault_attachment_v1'):
        return None
    rows = db.execute('SELECT id,content,authentication FROM restored_vault_attachment_v1 LIMIT 2').fetchall()
    try:
        if len(rows) != 1 or rows[0]['id'] != 1 or _has_table(db, 'restored_missing_vault_v1'):
            raise ValueError()
        content = json.loads(rows[0]['content'], object_pairs_hook=_unique_object)
        fields = {'version', 'receipt_auth', 'inbox_dir', 'key_dir', 'inbox_id', 'key_id', 'material'}
        config = _load(runtime.root / 'runtime.json')
        if (not isinstance(content, dict) or set(content) != fields or type(content['version']) is not int
                or content['version'] != 1 or config.get('service_mode') is not True
                or config.get('restored_missing_vault') is not True
                or any(not isinstance(content[name], str) or not content[name] for name in
                       ('receipt_auth', 'inbox_dir', 'key_dir', 'inbox_id', 'key_id'))
                or not isinstance(content['material'], list) or len(content['material']) != 3
                or any(not isinstance(item, str) for item in content['material'])
                or not hmac.compare_digest(content['receipt_auth'], _receipt(runtime))):
            raise ValueError()
        expected = hmac.digest(runtime._fingerprint_key, b'evernook-vault-attachment-v1\0'
                               + _json(content).encode(), hashlib.sha256).hex()
        if not isinstance(rows[0]['authentication'], str) or not hmac.compare_digest(rows[0]['authentication'], expected):
            raise ValueError()
        return content
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise MemoryFailure('protected_recovery_proof_invalid') from None


def _read_state(runtime: MemoryRuntime, db: sqlite3.Connection) -> bool:
    config = _load(runtime.root / 'runtime.json')
    if attachment(runtime, db) is not None:
        _check_projections(db, require_protected=False)
        return False
    if not _has_table(db, 'restored_missing_vault_v1'):
        if config.get('restored_missing_vault') is True:
            raise MemoryFailure('protected_recovery_proof_invalid')
        if config.get('service_mode') is True and (runtime.root / 'recovery-receipt.json').exists():
            content, _ = _signed_receipt(runtime)
            if content['protected_references'] is True:
                raise MemoryFailure('protected_recovery_proof_invalid')
        return False
    rows = db.execute('SELECT id,receipt_auth FROM restored_missing_vault_v1 LIMIT 2').fetchall()
    if not rows:
        raise MemoryFailure('protected_recovery_proof_invalid')
    if len(rows) != 1 or rows[0]['id'] != 1 or not isinstance(rows[0]['receipt_auth'], str):
        raise MemoryFailure('protected_recovery_proof_invalid')
    if not hmac.compare_digest(rows[0]['receipt_auth'], _receipt(runtime)):
        raise MemoryFailure('protected_recovery_proof_invalid')
    if config.get('service_mode') is True and config.get('restored_missing_vault') is not True:
        raise MemoryFailure('protected_recovery_proof_invalid')
    _check_projections(db, require_protected=False)
    return True


def read_state(runtime: MemoryRuntime, db: sqlite3.Connection) -> bool:
    try:
        return _read_state(runtime, db)
    except sqlite3.Error:
        raise MemoryFailure('protected_recovery_proof_invalid') from None


def record_state(runtime: MemoryRuntime, db: sqlite3.Connection) -> None:
    """Caller holds the exclusive ownership gate, administrator scope and SQLite write lock."""
    try:
        authentication = _receipt(runtime)
        _check_projections(db, require_protected=True)
        db.execute('CREATE TABLE IF NOT EXISTS restored_missing_vault_v1 (id INTEGER PRIMARY KEY CHECK(id=1), receipt_auth TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO restored_missing_vault_v1 VALUES (1,?)', (authentication,))
        if not read_state(runtime, db):
            raise MemoryFailure('protected_recovery_proof_invalid')
    except sqlite3.Error:
        raise MemoryFailure('protected_recovery_proof_invalid') from None
