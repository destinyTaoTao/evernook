"""Authenticated encrypted snapshot of one explicitly bound protected inbox."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import BinaryIO, Callable, cast
from uuid import uuid4

from nacl import pwhash
from nacl.secret import SecretBox

from .backup import _encrypt, _writer
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _json
from .vault_migration import _authorize_admin, _file_bytes, _materials, _snapshot
from .vault_crypto import _json_object


@dataclass(frozen=True)
class VaultBackupStatus:
    backup_id: str
    created_at: str
    protected_objects: int
    verification: str = 'signature_and_links_only'
    recovery_tested: bool = False


class _CheckedReader:
    def __init__(self, source: BinaryIO, check: Callable[[], None]):
        self.source = source
        self.check = check

    def read(self, size: int = -1) -> bytes:
        self.check()
        return self.source.read(size)


def _validate_recovery_structure(raw: bytes) -> None:
    value = _json_object(raw)
    try:
        if (set(value) != {'format', 'version', 'kdf', 'salt', 'ciphertext'}
                or value['format'] != 'evernook-protected-recovery' or type(value['version']) is not int
                or value['version'] != 1 or value['kdf'] != 'argon2id-interactive-v1'
                or not isinstance(value['salt'], str) or not isinstance(value['ciphertext'], str)):
            raise ValueError()
        salt, ciphertext = bytes.fromhex(value['salt']), bytes.fromhex(value['ciphertext'])
        if (len(value['salt']) != len(salt) * 2 or len(salt) != pwhash.argon2id.SALTBYTES
                or len(value['ciphertext']) != len(ciphertext) * 2
                or not SecretBox.NONCE_SIZE + SecretBox.MACBYTES <= len(ciphertext) <= 16384):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise MemoryFailure('invalid_vault_recovery') from None


def create_vault_backup(runtime: MemoryRuntime, inbox: ProtectedInbox, access: Access, destination: Path,
                        backup_keys: Path, *, timeout: float = 120,
                        cancel: threading.Event | None = None) -> VaultBackupStatus:
    """Trusted admin, one bound inbox. Does not unlock vault or include its password."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_vault_backup_request')
    runtime._require_scope(access, 'admin', 'vault_backup')
    if inbox.runtime.root != runtime.root:
        raise MemoryFailure('protected_binding_mismatch')
    destination, backup_keys = external(destination), external(backup_keys)
    roots = (runtime.root, inbox.root, inbox.key_root, backup_keys)
    if any(destination.is_relative_to(root) or root.is_relative_to(destination) for root in roots):
        raise MemoryFailure('unsafe_backup_path')
    if any(backup_keys.is_relative_to(root) or root.is_relative_to(backup_keys)
           for root in (runtime.root, inbox.root, inbox.key_root)):
        raise MemoryFailure('unsafe_backup_path')
    if destination.exists():
        raise MemoryFailure('backup_destination_exists')
    key_id, recipient, signer = _writer(backup_keys)
    deadline = time.monotonic() + timeout
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_backup_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_backup_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()

    try:
        with runtime._connection(ownership_exclusive=True, check=remaining) as core:
            core.execute('BEGIN IMMEDIATE')
            _authorize_admin(runtime, access)
            with inbox._connection(gate_db=core) as store:
                store.execute('BEGIN IMMEDIATE')
                material_before = _materials(inbox)
                snapshot = _snapshot(runtime, inbox, core, store, private=None, deadline=deadline,
                                     require_readable_entries=False, cancel=cancel, include_link_digest=True)
                if snapshot['material'] != material_before:
                    raise MemoryFailure('protected_migration_material_changed')
                with tempfile.TemporaryDirectory(prefix='.evernook-vault-backup-', dir=destination.parent) as temporary:
                    work = Path(temporary) / 'work'
                    staging = Path(temporary) / 'package'
                    provision_private_directory(work)
                    provision_private_directory(staging)
                    snapshot_file = work / 'inbox.sqlite3'
                    def progress(_status: int, _remaining: int, _total: int) -> None:
                        check()
                    with closing(sqlite3.connect((inbox.root / 'inbox.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
                        with closing(sqlite3.connect(snapshot_file)) as target:
                            source.backup(target, pages=256, progress=progress, sleep=.05)
                            target.execute('PRAGMA trusted_schema=OFF')
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
                    check()
                    if _materials(inbox) != material_before:
                        raise MemoryFailure('protected_migration_material_changed')
                    status = VaultBackupStatus(str(uuid4()), datetime.now(timezone.utc).isoformat(), cast(int, snapshot['objects']))
                    binding = {**{name: snapshot[name] for name in ('ledger_id', 'inbox_id', 'key_id', 'objects', 'link_digest')},
                               'created_at': status.created_at}
                    values = {'binding': _json(binding).encode(),
                              'receiving': _file_bytes(inbox.key_root / 'receiving.json', 65536),
                              'writer': _file_bytes(inbox.key_root / 'writer.key', 65536),
                              'recovery': _file_bytes(inbox.key_root / 'recovery.json', 65536)}
                    _validate_recovery_structure(values['recovery'])
                    files: dict[str, object] = {}
                    for name, raw in values.items():
                        check()
                        aad = _json({'backup_id': status.backup_id, 'backup_key_id': key_id, 'file': name}).encode()
                        files[name] = _encrypt(cast(BinaryIO, _CheckedReader(io.BytesIO(raw), check)), staging / (name + '.enc'), recipient, aad)
                    aad = _json({'backup_id': status.backup_id, 'backup_key_id': key_id, 'file': 'inbox'}).encode()
                    with snapshot_file.open('rb') as source_file:
                        files['inbox'] = _encrypt(cast(BinaryIO, _CheckedReader(source_file, check)), staging / 'inbox.enc', recipient, aad)
                    check()
                    if _materials(inbox) != material_before:
                        raise MemoryFailure('protected_migration_material_changed')
                    current_id, current_recipient, current_signer = _writer(backup_keys)
                    if (current_id != key_id or bytes(current_recipient) != bytes(recipient)
                            or bytes(current_signer.verify_key) != bytes(signer.verify_key)):
                        raise MemoryFailure('backup_materials_changed')
                    content = {'format': 'evernook-protected-backup', 'version': 1, 'backup_key_id': key_id,
                               'status': {'backup_id': status.backup_id}, 'files': files}
                    header = {'content': content, 'signature': signer.sign(_json(content).encode()).signature.hex()}
                    with (staging / 'backup.json').open('xb') as output:
                        output.write(_json(header).encode())
                        output.flush()
                        os.fsync(output.fileno())
                    _authorize_admin(runtime, access)
                    check()
                    os.rename(staging, destination)
                store.execute('COMMIT')
            core.execute('COMMIT')
        return status
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise MemoryFailure('vault_backup_failed') from None
