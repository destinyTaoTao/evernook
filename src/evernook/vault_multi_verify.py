"""Non-activating preflight for an explicitly declared set of legacy inboxes."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any, Callable
from uuid import UUID

from .models import Access, MemoryFailure, Receipt
from .protected import ProtectedInbox, _require_protected_available
from .runtime import MemoryRuntime, _identities, _json, _load, _unique_object
from .vault_crypto import _decrypt_event, _unlock
from .paths import verify_private_directory
from .vault_migration import MAX_OBJECTS, MAX_SECONDS, _authorize_admin, _file_bytes, _materials


def _inspect(runtime: MemoryRuntime, access: Access | None,
             declared: tuple[tuple[ProtectedInbox, bytes | None], ...], *, decrypt: bool,
             require_complete: bool, locked: Callable[[dict[str, Any]], None] | None = None,
             require_readable: bool = True, unbounded_objects: bool = False,
             service_startup: bool = False) -> dict[str, Any]:
    if access is None and not (service_startup and runtime._service_owner):
        raise MemoryFailure('permission_denied')
    def authorize() -> None:
        if access is not None:
            _authorize_admin(runtime, access)
    if (not isinstance(declared, tuple) or not 2 <= len(declared) <= 16
            or any(not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], ProtectedInbox)
                   or item[0].runtime is not runtime or (not isinstance(item[1], bytes) if decrypt else item[1] is not None)
                   for item in declared)):
        raise MemoryFailure('invalid_protected_inbox_set')
    inboxes = sorted(declared, key=lambda item: item[0].inbox_id)
    ids = [item[0].inbox_id for item in inboxes]
    try:
        if any(str(UUID(value)) != value for value in ids):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise MemoryFailure('protected_binding_mismatch') from None
    keys = [str(item[0].public['key_id']) for item in inboxes]
    paths = [path for inbox, _ in inboxes for path in (inbox.root, inbox.key_root)]
    paths.append(runtime.root)
    if (len(set(ids)) != len(ids) or len(set(keys)) != len(keys) or len(set(paths)) != len(paths)
            or any(left.is_relative_to(right) for left in paths for right in paths if left != right)):
        raise MemoryFailure('invalid_protected_inbox_set')
    deadline = time.monotonic() + MAX_SECONDS
    def check() -> None:
        if time.monotonic() >= deadline:
            raise MemoryFailure('protected_migration_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()
    with runtime._connection(ownership_exclusive=True, check=remaining) as core:
        core.execute('BEGIN IMMEDIATE')
        authorize()
        _require_protected_available(runtime, core)
        key_path = runtime.root / 'idempotency.key'
        client_path = runtime.root / 'client.json'
        verify_private_directory(runtime.root, extra_files=(key_path, client_path))
        if not hmac.compare_digest(_file_bytes(key_path, 32), runtime._fingerprint_key):
            raise MemoryFailure('protected_migration_material_changed')
        if not core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_inbox_registry_v1'").fetchone():
            raise MemoryFailure('protected_cutover_requires_migration')
        registered = {(str(row['inbox_id']), str(row['key_id'])) for row in
                      core.execute('SELECT inbox_id,key_id FROM protected_inbox_registry_v1')}
        if registered != set(zip(ids, keys)):
            raise MemoryFailure('protected_cutover_requires_migration')
        ledger = core.execute('SELECT ledger_id FROM ledger_identity_v1 WHERE id=1').fetchone()
        if ledger is None:
            raise MemoryFailure('protected_cutover_requires_migration')
        has_links = bool(core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_links_v1'").fetchone())
        core.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            links = int(core.execute('SELECT count(*) FROM protected_links_v1').fetchone()[0]) if has_links else 0
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
        identities = _identities(_load(runtime.root / 'client.json'))
        clients = hmac.digest(runtime._fingerprint_key, b'protected-multi-clients-v1\0'
                              + _file_bytes(client_path, 1048576), 'sha256').hex()
        readable = {identity.entry for identity in identities.values() if 'recall' in identity.scopes}
        total = linked = pending = acknowledged = 0
        sources: set[str] = set()
        summaries: list[dict[str, object]] = []
        with ExitStack() as stack:
            stores = []
            for inbox, password in inboxes:
                check()
                material = _materials(inbox)
                inbox._writer()
                private = _unlock(inbox, password) if password is not None else None
                store = stack.enter_context(inbox._connection(gate_db=core))
                store.execute('BEGIN IMMEDIATE')
                stores.append((inbox, private, store, material))
            for _, _, store, _ in stores:
                for source_row in store.execute('SELECT source FROM objects'):
                    check()
                    if ((not unbounded_objects and len(sources) >= MAX_OBJECTS) or source_row['source'] in sources):
                        raise MemoryFailure('protected_cutover_requires_migration')
                    sources.add(source_row['source'])
            for inbox, private, store, material in stores:
                count = inbox_pending = 0
                digest = hashlib.sha256()
                settings = store.execute('SELECT * FROM settings WHERE singleton=1').fetchone()
                binding = hmac.digest(runtime._fingerprint_key, b'evernook-protected-inbox-v1', 'sha256')
                if (settings is None or settings['inbox_id'] != inbox.inbox_id
                        or json.loads(settings['public'], object_pairs_hook=_unique_object) != inbox.public
                        or not hmac.compare_digest(bytes(settings['runtime_binding']), binding)):
                    raise MemoryFailure('protected_binding_mismatch')
                for row in store.execute('SELECT * FROM objects ORDER BY id'):
                    check()
                    total += 1; count += 1
                    if not unbounded_objects and total > MAX_OBJECTS:
                        raise MemoryFailure('protected_cutover_requires_migration')
                    is_pending = row['state'] == 'protected_pending_link' and row['receipt'] is None
                    if is_pending and require_complete:
                        raise MemoryFailure('protected_cutover_requires_migration')
                    if not is_pending and (row['state'] != 'protected_linked' or row['receipt'] is None):
                        raise MemoryFailure('protected_object_corrupt')
                    _, projection = inbox._verified(row)
                    if require_readable and projection.source.entry not in readable:
                        raise MemoryFailure('protected_migration_unreachable_entry')
                    if private is not None:
                        _decrypt_event(inbox, row, private)
                    saved = core.execute('''SELECT m.id,m.event,m.receipt,m.fingerprint,l.reference
                        FROM messages m JOIN protected_links_v1 l ON l.message_id=m.id WHERE m.source=?''',
                        (row['source'],)).fetchone() if has_links else None
                    if saved is None:
                        if not is_pending or core.execute('SELECT 1 FROM messages WHERE source=?', (row['source'],)).fetchone():
                            raise MemoryFailure('protected_link_unavailable')
                        pending += 1; inbox_pending += 1
                        continue
                    try:
                        receipt = Receipt(**json.loads(saved['receipt'], object_pairs_hook=_unique_object))
                        if (receipt.message_id != saved['id'] or receipt.status != 'projection_only'
                                or receipt.representation != 'projection' or receipt.original_saved is not False):
                            raise ValueError()
                    except (ValueError, TypeError, KeyError):
                        raise MemoryFailure('protected_object_corrupt') from None
                    if ((not is_pending and row['receipt'] != saved['receipt'])
                            or saved['event'] != _json(asdict(projection))
                            or not hmac.compare_digest(bytes(saved['fingerprint']), bytes(row['fingerprint']))
                            or saved['reference'] != inbox._reference(str(row['id']), str(row['envelope']))):
                        raise MemoryFailure('protected_link_unavailable')
                    linked += 1
                    state = runtime._state(core, str(saved['id']))
                    digest.update((_json([row['id'], row['source'], bytes(row['fingerprint']).hex(),
                        hashlib.sha256(str(row['envelope']).encode()).hexdigest(), row['receipt'],
                        saved['id'], saved['reference'], asdict(state)]) + '\n').encode())
                    if is_pending:
                        pending += 1; inbox_pending += 1; acknowledged += 1
                if _materials(inbox) != material:
                    raise MemoryFailure('protected_migration_material_changed')
                path_binding = hmac.digest(runtime._fingerprint_key,
                    _json([str(inbox.root), str(inbox.key_root)]).encode(), 'sha256').hex()
                summaries.append({'inbox_id': inbox.inbox_id, 'key_id': str(inbox.public['key_id']),
                                  'objects': count, 'pending': inbox_pending, 'path_binding': path_binding,
                                  'material': material, 'object_digest': digest.hexdigest()})
            if linked != links:
                raise MemoryFailure('protected_cutover_requires_migration')
            check()
            authorize()
            current_clients = hmac.digest(runtime._fingerprint_key, b'protected-multi-clients-v1\0'
                                          + _file_bytes(client_path, 1048576), 'sha256').hex()
            if not hmac.compare_digest(current_clients, clients):
                raise MemoryFailure('protected_migration_material_changed')
            verify_private_directory(runtime.root, extra_files=(key_path, client_path))
            if not hmac.compare_digest(_file_bytes(key_path, 32), runtime._fingerprint_key):
                raise MemoryFailure('protected_migration_material_changed')
            snapshot = {'ledger_id': str(ledger['ledger_id']), 'clients': clients, 'inboxes': summaries,
                        'objects': total, 'linked_references': linked}
            if locked is not None:
                locked(snapshot)
            for _, _, store, _ in stores:
                store.execute('COMMIT')
        core.execute('COMMIT')
    public = [{'inbox_id': item['inbox_id'], 'objects': item['objects'], 'pending': item['pending']} for item in summaries]
    return {'inboxes': public, 'objects': total, 'linked_references': linked, 'pending': pending,
            'pending_with_link': acknowledged, 'migration_authorized': False, 'unknown_inboxes_discoverable': False}


def verify_declared(runtime: MemoryRuntime, access: Access,
                    declared: tuple[tuple[ProtectedInbox, bytes], ...]) -> dict[str, Any]:
    """Validate 2–16 inboxes without changing protected assets or activating service.

    Authorization may write ordinary audit records and SQLite journal files.
    """
    runtime._require_scope(access, 'admin', 'protected_multi_verify')
    return _inspect(runtime, access, declared, decrypt=True, require_complete=False)
