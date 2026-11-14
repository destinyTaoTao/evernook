"""Trusted, bounded verification and proof for a single protected inbox cutover."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from nacl.public import PrivateKey

from .models import Access, MemoryFailure, Receipt
from .paths import verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_json, _identities, _json, _load, _unique_object
from .vault_crypto import _decrypt_event, _unlock


PROOF_NAME = 'protected-cutover-proof.json'
MAX_OBJECTS = 10000
MAX_SECONDS = 120


def _authorize_admin(runtime: MemoryRuntime, access: Access) -> None:
    if 'admin' not in runtime._authorize(access).scopes:
        raise MemoryFailure('permission_denied')


def _file_bytes(path: Path, maximum: int) -> bytes:
    try:
        with path.open('rb') as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise MemoryFailure('protected_migration_material_invalid')
        return data
    except OSError:
        raise MemoryFailure('protected_migration_material_invalid') from None


def _materials(inbox: ProtectedInbox) -> list[str]:
    keys = tuple(inbox.key_root / name for name in ('receiving.json', 'writer.key', 'recovery.json'))
    verify_private_directory(inbox.key_root, extra_files=keys)
    return [hmac.digest(inbox.runtime._fingerprint_key, b'protected-cutover-material-v1\0'
                        + path.name.encode() + b'\0' + _file_bytes(path, 65536), hashlib.sha256).hex() for path in keys]


def _snapshot(runtime: MemoryRuntime, inbox: ProtectedInbox, core: sqlite3.Connection, store: sqlite3.Connection, *,
              private: PrivateKey | None, deadline: float, require_readable_entries: bool = True,
              cancel: threading.Event | None = None, include_link_digest: bool = False) -> dict[str, object]:
    """Called only with the exclusive ownership gate and a core transaction held."""
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_backup_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('protected_migration_timeout')

    check()
    registry = core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_inbox_registry_v1'").fetchone()
    entries = core.execute('SELECT inbox_id,key_id FROM protected_inbox_registry_v1 ORDER BY inbox_id').fetchall() if registry else []
    if len(entries) != 1 or entries[0]['inbox_id'] != inbox.inbox_id or entries[0]['key_id'] != inbox.public['key_id']:
        raise MemoryFailure('protected_cutover_requires_migration')
    ledger = core.execute('SELECT ledger_id FROM ledger_identity_v1 WHERE id=1').fetchone()
    if ledger is None:
        raise MemoryFailure('protected_cutover_requires_migration')
    material = _materials(inbox)
    inbox._writer()  # Verify that the service can sign new protected writes.
    client_binding = hmac.digest(runtime._fingerprint_key, b'protected-cutover-clients-v1\0'
                                 + _file_bytes(runtime.root / 'client.json', 1048576), hashlib.sha256).hex()
    identities = _identities(_load(runtime.root / 'client.json'))
    readable_entries = {identity.entry for identity in identities.values() if 'recall' in identity.scopes}
    digest = hashlib.sha256()
    link_digest = hashlib.sha256()
    count = 0
    settings = store.execute('SELECT * FROM settings WHERE singleton=1').fetchone()
    binding = hmac.digest(runtime._fingerprint_key, b'evernook-protected-inbox-v1', hashlib.sha256)
    if (settings is None or settings['inbox_id'] != inbox.inbox_id
            or json.loads(settings['public'], object_pairs_hook=_unique_object) != inbox.public
            or not hmac.compare_digest(bytes(settings['runtime_binding']), binding)):
        raise MemoryFailure('protected_binding_mismatch')
    has_links = core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_links_v1'").fetchone()
    link_count = int(core.execute('SELECT count(*) FROM protected_links_v1').fetchone()[0]) if has_links else 0
    core.set_progress_handler(lambda: int(time.monotonic() >= deadline or (cancel is not None and cancel.is_set())), 1000)
    try:
        if has_links:
            orphan = core.execute("""SELECT 1 FROM messages m LEFT JOIN protected_links_v1 l ON l.message_id=m.id
                WHERE json_extract(m.event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' AND l.message_id IS NULL LIMIT 1""").fetchone()
        else:
            orphan = core.execute("SELECT 1 FROM messages WHERE json_extract(event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        check()
        raise
    finally:
        core.set_progress_handler(None, 0)
    if orphan:
        raise MemoryFailure('protected_link_unavailable')
    for row in store.execute('SELECT * FROM objects ORDER BY id'):
        check()
        count += 1
        if count > MAX_OBJECTS or row['state'] != 'protected_linked' or row['receipt'] is None or not has_links:
            raise MemoryFailure('protected_cutover_requires_migration')
        _, projection = inbox._verified(row)
        if require_readable_entries and projection.source.entry not in readable_entries:
            raise MemoryFailure('protected_migration_unreachable_entry')
        if private is not None:
            _decrypt_event(inbox, row, private)
        saved = core.execute('''SELECT m.id,m.event,m.receipt,m.fingerprint,l.reference
            FROM messages m JOIN protected_links_v1 l ON l.message_id=m.id WHERE m.source=?''', (row['source'],)).fetchone()
        if saved is None:
            raise MemoryFailure('protected_link_unavailable')
        try:
            receipt = Receipt(**json.loads(saved['receipt'], object_pairs_hook=_unique_object))
            valid_receipt = (receipt.message_id == saved['id'] and receipt.status == 'projection_only'
                             and receipt.representation == 'projection' and receipt.original_saved is False)
        except (ValueError, TypeError, KeyError):
            raise MemoryFailure('protected_object_corrupt') from None
        if (not valid_receipt or saved['receipt'] != row['receipt'] or saved['event'] != _json(asdict(projection))
                or not hmac.compare_digest(bytes(saved['fingerprint']), bytes(row['fingerprint']))
                or saved['reference'] != inbox._reference(str(row['id']), str(row['envelope']))):
            raise MemoryFailure('protected_link_unavailable')
        state = runtime._state(core, str(saved['id']))
        link_digest.update((_json([row['id'], row['source'], bytes(row['fingerprint']).hex(),
            hashlib.sha256(str(row['envelope']).encode()).hexdigest(), row['receipt'], saved['id'], saved['reference']]) + '\n').encode())
        digest.update((_json([row['id'], row['source'], bytes(row['fingerprint']).hex(),
            hashlib.sha256(str(row['envelope']).encode()).hexdigest(), row['receipt'], saved['id'], saved['reference'],
            asdict(state)]) + '\n').encode())
    if count != link_count:
        raise MemoryFailure('protected_cutover_requires_migration')
    check()
    result: dict[str, object] = {'ledger_id': str(ledger['ledger_id']), 'inbox_id': inbox.inbox_id, 'key_id': str(inbox.public['key_id']),
            'path_binding': hmac.digest(runtime._fingerprint_key, _json([str(inbox.root), str(inbox.key_root)]).encode(), hashlib.sha256).hex(),
            'material': material, 'clients': client_binding, 'objects': count, 'object_digest': digest.hexdigest()}
    if include_link_digest:
        result['link_digest'] = link_digest.hexdigest()
    return result


def _authentication(runtime: MemoryRuntime, value: dict[str, Any]) -> str:
    return hmac.digest(runtime._fingerprint_key, b'protected-cutover-proof-v1\0' + _json(value).encode(), hashlib.sha256).hex()


def _remove_proof(runtime: MemoryRuntime) -> None:
    proof_path = runtime.root / PROOF_NAME
    verify_private_directory(runtime.root, extra_files=(proof_path,))
    try:
        proof_path.unlink(missing_ok=True)
    except OSError:
        raise MemoryFailure('protected_migration_proof_unavailable') from None


def invalidate(runtime: MemoryRuntime, access: Access) -> None:
    """Discard an earlier derived proof before asking for fresh consent."""
    runtime._require_scope(access, 'admin', 'protected_migration_prepare')
    with runtime._connection(ownership_exclusive=True) as core:
        core.execute('BEGIN IMMEDIATE')
        _authorize_admin(runtime, access)
        _remove_proof(runtime)
        core.execute('COMMIT')


def prepare(runtime: MemoryRuntime, inbox: ProtectedInbox, access: Access, password: bytes, *,
            inventory_statement: str) -> dict[str, object]:
    """Verify every object under the cutover gate and save a short-lived proof."""
    runtime._require_scope(access, 'admin', 'protected_migration_prepare')
    deadline = time.monotonic() + MAX_SECONDS
    with runtime._connection(ownership_exclusive=True) as core:
        core.execute('BEGIN IMMEDIATE')
        _authorize_admin(runtime, access)
        _remove_proof(runtime)
        if inventory_statement != 'STOPPED_AND_INVENTORIED':
            raise MemoryFailure('protected_migration_inventory_unconfirmed')
        material_before = _materials(inbox)
        from .protected import _require_protected_available
        _require_protected_available(runtime, core)
        private = _unlock(inbox, password)
        with inbox._connection(gate_db=core) as store:
            store.execute('BEGIN IMMEDIATE')
            snapshot = _snapshot(runtime, inbox, core, store, private=private, deadline=deadline)
            if snapshot['material'] != material_before or _materials(inbox) != material_before:
                raise MemoryFailure('protected_migration_material_changed')
            _authorize_admin(runtime, access)
            now = datetime.now(timezone.utc)
            proof: dict[str, Any] = {'version': 1, 'owner': access.client_id, 'issued_at': now.isoformat(),
                'expires_at': (now + timedelta(minutes=10)).isoformat(), 'snapshot': snapshot}
            proof['authentication'] = _authentication(runtime, proof)
            _atomic_json(runtime.root / PROOF_NAME, proof)
            store.execute('COMMIT')
        core.execute('COMMIT')
    return {'verified_objects': snapshot['objects'], 'expires_at': proof['expires_at'],
            'proof_saved': True, 'migration_completed': False}


def verify_for_activation(runtime: MemoryRuntime, inbox: ProtectedInbox, access: Access,
                          core: sqlite3.Connection, store: sqlite3.Connection) -> None:
    """Verify a prepared proof while the service holds the exclusive gate."""
    _authorize_admin(runtime, access)
    path = runtime.root / PROOF_NAME
    verify_private_directory(runtime.root, extra_files=(path,))
    try:
        value: object = json.loads(_file_bytes(path, 8192).decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
    except (MemoryFailure, ValueError, UnicodeError):
        raise MemoryFailure('protected_migration_proof_invalid') from None
    if not isinstance(value, dict):
        raise MemoryFailure('protected_migration_proof_invalid')
    proof = value
    authentication = proof.pop('authentication', None)
    if (set(proof) != {'version', 'owner', 'issued_at', 'expires_at', 'snapshot'}
            or type(proof['version']) is not int or proof['version'] != 1 or proof['owner'] != access.client_id
            or not isinstance(authentication, str) or len(authentication) != 64
            or any(char not in '0123456789abcdef' for char in authentication)
            or not hmac.compare_digest(authentication, _authentication(runtime, proof))):
        raise MemoryFailure('protected_migration_proof_invalid')
    try:
        issued, expires = (datetime.fromisoformat(proof[key]) for key in ('issued_at', 'expires_at'))
        now = datetime.now(timezone.utc)
        if (issued.tzinfo is None or expires.tzinfo is None or not issued <= now <= expires
                or expires - issued > timedelta(minutes=10)):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise MemoryFailure('protected_migration_proof_invalid') from None
    snapshot = _snapshot(runtime, inbox, core, store, private=None, deadline=time.monotonic() + MAX_SECONDS)
    if snapshot != proof['snapshot'] or _materials(inbox) != snapshot['material']:
        raise MemoryFailure('protected_migration_proof_stale')
    if not issued <= datetime.now(timezone.utc) <= expires:
        raise MemoryFailure('protected_migration_proof_invalid')
    _authorize_admin(runtime, access)
