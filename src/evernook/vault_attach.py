"""Explicit offline reattachment of one restored and reverified vault."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import cast

from .missing_vault import _receipt, attachment, read_state
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_bytes, _atomic_json, _json
from .vault_crypto import _unlock
from .vault_migration import _authorize_admin, _materials, _snapshot
from .vault_restore import _stopped_service


@dataclass(frozen=True)
class VaultAttachment:
    inbox_dir: str
    key_dir: str
    protected_objects: int
    daily_admitted: bool = False


def _commit_attachment(core: sqlite3.Connection) -> None:
    core.execute('COMMIT')


def attach(config: Path, access: Access, container: Path, vault_password: bytes, *,
           cancel: threading.Event | None = None, timeout: float = 120) -> VaultAttachment:
    """Reverify a staged inbox against the locked restored ledger and bind it atomically."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_vault_attach_request')
    config, container = external(config), external(container)
    root = config.parent
    if container.is_relative_to(root) or root.is_relative_to(container):
        raise MemoryFailure('unsafe_backup_path')
    inbox_dir, key_dir = external(container / 'inbox'), external(container / 'keys')
    if inbox_dir.parent != container or key_dir.parent != container:
        raise MemoryFailure('unsafe_backup_path')
    deadline = time.monotonic() + timeout
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_attach_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_attach_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()

    check()
    publication_started = False
    try:
        with _stopped_service(root):
            runtime = MemoryRuntime(config, service_owner=True)
            _authorize_admin(runtime, access)
            with runtime._connection(ownership_exclusive=True, check=remaining) as core:
                core.execute('BEGIN IMMEDIATE')
                _authorize_admin(runtime, access)
                missing = read_state(runtime, core)
                previous = attachment(runtime, core)
                if not missing and previous is None:
                    raise MemoryFailure('protected_recovery_proof_invalid')
                if previous is not None and (previous['inbox_dir'] != str(inbox_dir) or previous['key_dir'] != str(key_dir)):
                    raise MemoryFailure('protected_attachment_mismatch')
                verify_private_directory(container)
                if {path.name for path in container.iterdir()} != {'inbox', 'keys'}:
                    raise MemoryFailure('protected_attachment_mismatch')
                with tempfile.TemporaryDirectory(prefix='.evernook-vault-attach-', dir=container.parent) as temporary:
                    clone = Path(temporary) / 'ledger'
                    provision_private_directory(clone)
                    with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                        with closing(sqlite3.connect(clone / 'memory.sqlite3')) as copy:
                            db.backup(copy, pages=256, progress=lambda *_: check(), sleep=.05)
                            copy.execute('DROP TABLE IF EXISTS restored_missing_vault_v1')
                            copy.execute('DROP TABLE IF EXISTS restored_vault_attachment_v1')
                            copy.commit()
                    _atomic_json(clone / 'runtime.json', {'schema_version': 1, 'data_dir': str(clone), 'busy_timeout_ms': 5000})
                    shutil.copyfile(root / 'client.json', clone / 'client.json')
                    _atomic_bytes(clone / 'idempotency.key', runtime._fingerprint_key)
                    isolated = MemoryRuntime(clone / 'runtime.json')
                    inbox = ProtectedInbox(isolated, inbox_dir, key_dir)
                    before = _materials(inbox)
                    private = _unlock(inbox, vault_password)
                    with isolated._connection(ownership_exclusive=True, check=remaining) as copied_core:
                        copied_core.execute('BEGIN IMMEDIATE')
                        with inbox._connection(gate_db=copied_core) as store:
                            store.execute('BEGIN IMMEDIATE')
                            store.set_progress_handler(lambda: int(time.monotonic() >= deadline
                                or (cancel is not None and cancel.is_set())), 1000)
                            try:
                                if [row[0] for row in store.execute('PRAGMA quick_check')] != ['ok']:
                                    raise MemoryFailure('vault_restore_integrity_failed')
                            except sqlite3.OperationalError:
                                check()
                                raise
                            finally:
                                store.set_progress_handler(None, 0)
                            snapshot = _snapshot(isolated, inbox, copied_core, store, private=private,
                                deadline=deadline, require_readable_entries=False, cancel=cancel, include_link_digest=True)
                            if _materials(inbox) != before:
                                raise MemoryFailure('protected_migration_material_changed')
                            store.execute('COMMIT')
                        copied_core.execute('COMMIT')
                    _authorize_admin(runtime, access)
                    check()
                    content = {'version': 1, 'receipt_auth': _receipt(runtime), 'inbox_dir': str(inbox_dir),
                               'key_dir': str(key_dir), 'inbox_id': snapshot['inbox_id'], 'key_id': snapshot['key_id'],
                               'material': before}
                    if previous is not None:
                        if previous != content:
                            raise MemoryFailure('protected_attachment_mismatch')
                    else:
                        authentication = hmac.digest(runtime._fingerprint_key, b'evernook-vault-attachment-v1\0'
                                                     + _json(content).encode(), hashlib.sha256).hex()
                        core.execute('CREATE TABLE restored_vault_attachment_v1 (id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL, authentication TEXT NOT NULL)')
                        core.execute('INSERT INTO restored_vault_attachment_v1 VALUES (1,?,?)', (_json(content), authentication))
                        core.execute('DROP TABLE restored_missing_vault_v1')
                    publication_started = True
                    _commit_attachment(core)
                    return VaultAttachment(str(inbox_dir), str(key_dir), cast(int, snapshot['objects']))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        if publication_started:
            raise MemoryFailure('vault_attach_outcome_unknown') from None
        raise MemoryFailure('vault_attach_failed') from None
