"""Offline verification of one encrypted vault backup into an isolated container."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import msvcrt
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Iterator, cast
from uuid import UUID

from nacl.exceptions import BadSignatureError, CryptoError

from .backup import _decrypt, _hex, _json_bytes, _unlock
from .missing_vault import read_state
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_bytes, _atomic_json, _json
from .vault_crypto import _unlock as unlock_vault
from .vault_migration import _authorize_admin, _snapshot
from .vault_workspace import workspace


@dataclass(frozen=True)
class VaultRestoreStage:
    backup_id: str
    protected_objects: int
    destination: str
    attached: bool = False
    daily_admitted: bool = False


@contextmanager
def _stopped_service(root: Path) -> Iterator[None]:
    lock_path = root / 'memory-service.lock'
    verify_private_directory(root, extra_files=(lock_path,))
    with lock_path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise MemoryFailure('service_already_running') from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def restore_stage(config: Path, access: Access, source: Path, destination: Path, recovery_file: Path,
                  backup_password: bytes, vault_password: bytes, *, cancel: threading.Event | None = None,
                  timeout: float = 120) -> VaultRestoreStage:
    """Trusted offline caller; a validated copy is not an attachment authorization."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_vault_restore_request')
    config, source, destination, recovery_file = map(external, (config, source, destination, recovery_file))
    root = config.parent
    if any(destination.is_relative_to(path) or path.is_relative_to(destination)
           for path in (root, source, recovery_file.parent)):
        raise MemoryFailure('unsafe_backup_path')
    if destination.exists():
        raise MemoryFailure('backup_destination_exists')
    deadline = time.monotonic() + timeout
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_restore_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_restore_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()

    check()
    try:
        with _stopped_service(root):
            runtime = MemoryRuntime(config, service_owner=True)
            _authorize_admin(runtime, access)
            with runtime._connection(ownership_exclusive=True, check=remaining) as core:
                core.execute('BEGIN IMMEDIATE')
                _authorize_admin(runtime, access)
                if not read_state(runtime, core):
                    raise MemoryFailure('protected_recovery_proof_invalid')
                key_id, private, sender = _unlock(recovery_file, backup_password)
                check()
                names = ('backup.json', 'binding.enc', 'receiving.enc', 'writer.enc', 'recovery.enc', 'inbox.enc')
                verify_private_directory(source, extra_files=tuple(source / name for name in names))
                if {item.name for item in source.iterdir()} != set(names):
                    raise MemoryFailure('invalid_backup')
                with (source / 'backup.json').open('rb') as f:
                    header = _json_bytes(f.read(65537), 65536)
                if set(header) != {'content', 'signature'} or not isinstance(header['content'], dict):
                    raise MemoryFailure('invalid_backup')
                content = header['content']
                sender.verify(_json(content).encode(), _hex(header['signature'], 64))
                if (set(content) != {'format', 'version', 'backup_key_id', 'status', 'files'}
                        or content['format'] != 'evernook-protected-backup' or type(content['version']) is not int
                        or content['version'] != 1 or content['backup_key_id'] != key_id
                        or not isinstance(content['status'], dict) or set(content['status']) != {'backup_id'}
                        or not isinstance(content['files'], dict)
                        or set(content['files']) != {'binding', 'receiving', 'writer', 'recovery', 'inbox'}):
                    raise MemoryFailure('invalid_backup')
                backup_id = content['status']['backup_id']
                if not isinstance(backup_id, str) or str(UUID(backup_id)) != backup_id:
                    raise MemoryFailure('invalid_backup')
                with workspace(runtime, destination.parent, 'restore') as temporary:
                    stage = temporary / 'vault'
                    clone = temporary / 'ledger'
                    provision_private_directory(stage)
                    provision_private_directory(clone)
                    inbox_dir, key_dir = stage / 'inbox', stage / 'keys'
                    provision_private_directory(inbox_dir)
                    provision_private_directory(key_dir)
                    targets = {'binding': clone / 'binding.json', 'receiving': key_dir / 'receiving.json',
                               'writer': key_dir / 'writer.key', 'recovery': key_dir / 'recovery.json',
                               'inbox': inbox_dir / 'inbox.sqlite3'}
                    for name, target in targets.items():
                        check()
                        info = content['files'][name]
                        maximum = 1073741824 if name == 'inbox' else 65536
                        if (not isinstance(info, dict) or type(info.get('plaintext_size')) is not int
                                or not 0 <= info['plaintext_size'] <= maximum
                                or (source / (name + '.enc')).stat().st_size > maximum * 2 + 65536):
                            raise MemoryFailure('invalid_backup')
                        aad = _json({'backup_id': backup_id, 'backup_key_id': key_id, 'file': name}).encode()
                        with target.open('xb') as output:
                            _decrypt(source / (name + '.enc'), output, private, info, aad, check=check)
                            output.flush()
                            os.fsync(output.fileno())
                    check()
                    binding = _json_bytes((clone / 'binding.json').read_bytes(), 65536)
                    if set(binding) != {'ledger_id', 'inbox_id', 'key_id', 'objects', 'link_digest', 'created_at'}:
                        raise MemoryFailure('invalid_backup')
                    # Copy the locked ordinary ledger so protected verification cannot bypass its real missing-vault guard.
                    with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                        with closing(sqlite3.connect(clone / 'memory.sqlite3')) as copy:
                            db.backup(copy, pages=256, progress=lambda *_: check(), sleep=.05)
                            copy.execute('DROP TABLE IF EXISTS restored_missing_vault_v1')
                            copy.commit()
                    _atomic_json(clone / 'runtime.json', {'schema_version': 1, 'data_dir': str(clone), 'busy_timeout_ms': 5000})
                    shutil.copyfile(root / 'client.json', clone / 'client.json')
                    _atomic_bytes(clone / 'idempotency.key', runtime._fingerprint_key)
                    isolated = MemoryRuntime(clone / 'runtime.json')
                    inbox = ProtectedInbox(isolated, inbox_dir, key_dir)
                    vault_private = unlock_vault(inbox, vault_password)
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
                            snapshot = _snapshot(isolated, inbox, copied_core, store, private=vault_private,
                                deadline=deadline, require_readable_entries=False, cancel=cancel, include_link_digest=True)
                            store.execute('COMMIT')
                        copied_core.execute('COMMIT')
                    if any(binding[name] != snapshot[name] for name in ('ledger_id', 'inbox_id', 'key_id', 'objects', 'link_digest')):
                        raise MemoryFailure('vault_backup_ledger_mismatch')
                    _authorize_admin(runtime, access)
                    check()
                    os.rename(stage, destination)
                core.execute('COMMIT')
                return VaultRestoreStage(backup_id, cast(int, snapshot['objects']), str(destination))
    except (BadSignatureError, CryptoError):
        raise MemoryFailure('backup_authentication_failed') from None
    except MemoryFailure:
        if destination.exists():
            raise MemoryFailure('vault_restore_outcome_unknown') from None
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        if destination.exists():
            raise MemoryFailure('vault_restore_outcome_unknown') from None
        raise MemoryFailure('vault_restore_failed') from None
