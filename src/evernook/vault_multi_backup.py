"""Encrypted, authenticated backup of one complete bound inbox set."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, BinaryIO, cast
from uuid import uuid4

from .backup import _encrypt, _writer
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _json
from .vault_backup import _CheckedReader, _validate_recovery_structure
from .vault_migration import _authorize_admin, _file_bytes, _materials
from .vault_multi_binding import _content, read
from .vault_multi_verify import _inspect
from .vault_workspace import workspace


@dataclass(frozen=True)
class MultiVaultBackupStatus:
    backup_id: str
    created_at: str
    inboxes: int
    protected_objects: int
    verification: str = 'signature_and_links_only'
    recovery_tested: bool = False
    restore_supported: bool = False


def create_multi_backup(runtime: MemoryRuntime, primary: ProtectedInbox,
                        inboxes: tuple[ProtectedInbox, ...], access: Access, destination: Path,
                        backup_keys: Path, *, timeout: float = 120,
                        cancel: threading.Event | None = None) -> MultiVaultBackupStatus:
    """Back up every bound inbox without unlocking any vault or changing its contents."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_vault_backup_request')
    runtime._require_scope(access, 'admin', 'vault_backup')
    destination, backup_keys = external(destination), external(backup_keys)
    roots = [runtime.root, backup_keys, *(path for inbox in inboxes for path in (inbox.root, inbox.key_root))]
    if any(destination.is_relative_to(root) or root.is_relative_to(destination) for root in roots):
        raise MemoryFailure('unsafe_backup_path')
    if any(backup_keys.is_relative_to(root) or root.is_relative_to(backup_keys) for root in roots if root != backup_keys):
        raise MemoryFailure('unsafe_backup_path')
    if destination.exists():
        raise MemoryFailure('backup_destination_exists')
    key_id, recipient, signer = _writer(backup_keys)
    deadline = time.monotonic() + timeout
    publication_started = False
    result: MultiVaultBackupStatus | None = None

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_backup_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_backup_timeout')

    def locked(snapshot: dict[str, Any], core: sqlite3.Connection, stores: list[Any]) -> None:
        nonlocal publication_started, result
        check()
        if read(runtime) != _content(snapshot, inboxes, primary):
            raise MemoryFailure('protected_multi_binding_mismatch')
        # The current single-inbox format's link digest deliberately excludes
        # mutable visibility state, so later suppression remains authoritative.
        members: list[dict[str, Any]] = []
        status = MultiVaultBackupStatus(str(uuid4()), datetime.now(timezone.utc).isoformat(),
                                        len(stores), int(snapshot['objects']))
        with workspace(runtime, destination.parent, 'multi') as temporary:
            vaults, package = temporary / 'vaults', temporary / 'package'
            provision_private_directory(vaults)
            provision_private_directory(package)
            files: dict[str, object] = {}
            def encrypt(name: str, source: BinaryIO) -> None:
                check()
                aad = _json({'backup_id': status.backup_id, 'backup_key_id': key_id, 'file': name}).encode()
                files[name] = _encrypt(cast(BinaryIO, _CheckedReader(source, check)), package / (name + '.enc'), recipient, aad, check=check)

            for index, (inbox, _private, store, material) in enumerate(stores):
                check()
                slot = f'{index:02d}'
                slot_dir = vaults / slot
                provision_private_directory(slot_dir)
                inbox_dir = slot_dir / 'inbox'
                provision_private_directory(inbox_dir)
                digest = hashlib.sha256()
                for row in store.execute('SELECT * FROM objects ORDER BY id'):
                    check()
                    saved = core.execute('''SELECT m.id,l.reference FROM messages m JOIN protected_links_v1 l
                                            ON l.message_id=m.id WHERE m.source=?''', (row['source'],)).fetchone()
                    if saved is None:
                        raise MemoryFailure('protected_link_unavailable')
                    digest.update((_json([row['id'], row['source'], bytes(row['fingerprint']).hex(),
                        hashlib.sha256(str(row['envelope']).encode()).hexdigest(), row['receipt'],
                        saved['id'], saved['reference']]) + '\n').encode())
                summary = next(item for item in snapshot['inboxes'] if item['inbox_id'] == inbox.inbox_id)
                members.append({'slot': slot, 'inbox_id': inbox.inbox_id, 'key_id': str(inbox.public['key_id']),
                                'objects': summary['objects'], 'link_digest': digest.hexdigest()})
                snapshot_file = inbox_dir / 'inbox.sqlite3'
                if (inbox.root / 'inbox.sqlite3').stat().st_size > 1073741824:
                    raise MemoryFailure('vault_backup_too_large')
                with closing(sqlite3.connect((inbox.root / 'inbox.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(snapshot_file)) as target:
                        source.backup(target, pages=256, progress=lambda *_: check(), sleep=.05)
                        target.set_progress_handler(lambda: int(time.monotonic() >= deadline
                            or (cancel is not None and cancel.is_set())), 1000)
                        try:
                            if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                                raise MemoryFailure('vault_backup_integrity_failed')
                        except sqlite3.OperationalError:
                            check()
                            raise
                        finally:
                            target.set_progress_handler(None, 0)
                if snapshot_file.stat().st_size > 1073741824:
                    raise MemoryFailure('vault_backup_too_large')
                for name, filename in (('receiving', 'receiving.json'), ('writer', 'writer.key'), ('recovery', 'recovery.json')):
                    raw = _file_bytes(inbox.key_root / filename, 65536)
                    if name == 'recovery':
                        _validate_recovery_structure(raw)
                    encrypt(f'{slot}-{name}', io.BytesIO(raw))
                with snapshot_file.open('rb') as source_file:
                    encrypt(f'{slot}-inbox', source_file)
                if _materials(inbox) != material:
                    raise MemoryFailure('protected_migration_material_changed')
            binding = {'ledger_id': snapshot['ledger_id'], 'primary': primary.inbox_id,
                       'created_at': status.created_at, 'inboxes': members}
            encrypt('binding', io.BytesIO(_json(binding).encode()))
            current_id, current_recipient, current_signer = _writer(backup_keys)
            if (current_id != key_id or bytes(current_recipient) != bytes(recipient)
                    or bytes(current_signer.verify_key) != bytes(signer.verify_key)):
                raise MemoryFailure('backup_materials_changed')
            content = {'format': 'evernook-protected-multi-backup', 'version': 1, 'backup_key_id': key_id,
                       'status': {'backup_id': status.backup_id}, 'files': files}
            header = {'content': content, 'signature': signer.sign(_json(content).encode()).signature.hex()}
            with (package / 'backup.json').open('xb') as output:
                output.write(_json(header).encode())
                output.flush()
                os.fsync(output.fileno())
            _authorize_admin(runtime, access)
            check()
            if any(_materials(inbox) != material for inbox, _private, _store, material in stores):
                raise MemoryFailure('protected_migration_material_changed')
            publication_started = True
            os.rename(package, destination)
        result = status

    try:
        _inspect(runtime, access, tuple((inbox, None) for inbox in inboxes), decrypt=False,
                 require_complete=True, require_readable=False, locked_stores=locked, cancel=cancel,
                 deadline_limit=deadline, check_extra=check)
        assert result is not None
        return result
    except MemoryFailure:
        if publication_started:
            raise MemoryFailure('vault_backup_outcome_unknown') from None
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError):
        if publication_started:
            raise MemoryFailure('vault_backup_outcome_unknown') from None
        raise MemoryFailure('vault_backup_failed') from None
