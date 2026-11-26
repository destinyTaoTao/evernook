"""Offline verification of one encrypted vault backup into an isolated container."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, cast
from uuid import UUID

from nacl.exceptions import BadSignatureError, CryptoError

from .backup import _decrypt, _hex, _json_bytes, _unlock
from .missing_vault import read_state
from .models import Access, MemoryFailure
from .paths import external, verify_private_directory
from .platform.service_gate import single_service_writer
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _json
from .secure_ntfs import HeldFile, PrivateTree
from .vault_crypto import _unlock as unlock_vault
from .vault_migration import _authorize_admin, _snapshot
from .vault_workspace import secure_workspace


def _digest_of(stream: Any) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _copy_between(source: HeldFile, target: HeldFile) -> None:
    source.stream.seek(0)
    target.stream.seek(0)
    while True:
        chunk = source.stream.read(1024 * 1024)
        if not chunk:
            break
        target.stream.write(chunk)
    target.stream.flush()
    os.fsync(target.stream.fileno())


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
        with single_service_writer(lock):
            yield


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
                with secure_workspace(runtime, destination.parent, 'restore', core=core) as temporary:
                    stage_dir = temporary.mkdir('vault')
                    clone_dir = temporary.mkdir('ledger')
                    clone = clone_dir.path
                    inbox_holder, key_holder = stage_dir.mkdir('inbox'), stage_dir.mkdir('keys')
                    staged: dict[str, HeldFile] = {}
                    try:
                        for name, holder, filename in (('receiving', key_holder, 'receiving.json'),
                                                       ('writer', key_holder, 'writer.key'),
                                                       ('recovery', key_holder, 'recovery.json'),
                                                       ('inbox', inbox_holder, 'inbox.sqlite3'),
                                                       ('binding', clone_dir, 'binding.json')):
                            check()
                            info = content['files'][name]
                            maximum = 1073741824 if name == 'inbox' else 65536
                            if (not isinstance(info, dict) or type(info.get('plaintext_size')) is not int
                                    or not 0 <= info['plaintext_size'] <= maximum
                                    or (source / (name + '.enc')).stat().st_size > maximum * 2 + 65536):
                                raise MemoryFailure('invalid_backup')
                            aad = _json({'backup_id': backup_id, 'backup_key_id': key_id, 'file': name}).encode()
                            output = holder.create_file(filename, write_share=True)
                            staged[name] = output
                            _decrypt(source / (name + '.enc'), output.stream, private, info, aad, check=check)
                            output.stream.flush()
                            os.fsync(output.stream.fileno())
                        check()
                        staged['binding'].stream.seek(0)
                        binding = _json_bytes(staged['binding'].stream.read(), 65536)
                        if set(binding) != {'ledger_id', 'inbox_id', 'key_id', 'objects', 'link_digest', 'created_at'}:
                            raise MemoryFailure('invalid_backup')
                        # Copy the locked ordinary ledger so protected verification cannot bypass its real missing-vault guard.
                        staged['ledger'] = clone_dir.create_file('memory.sqlite3', write_share=True)
                        with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                            with closing(sqlite3.connect(clone / 'memory.sqlite3')) as copy:
                                db.backup(copy, pages=256, progress=lambda *_: check(), sleep=.05)
                                copy.execute('DROP TABLE IF EXISTS restored_missing_vault_v1')
                                copy.commit()
                        for filename, payload in (('runtime.json', _json({'schema_version': 1, 'data_dir': str(clone),
                                                                        'busy_timeout_ms': 5000}).encode()),
                                                  ('client.json', (root / 'client.json').read_bytes()),
                                                  ('idempotency.key', runtime._fingerprint_key)):
                            side = clone_dir.create_file(filename, write_share=True)
                            staged[f'clone:{filename}'] = side
                            side.stream.write(payload)
                            side.stream.flush()
                            os.fsync(side.stream.fileno())
                        isolated = MemoryRuntime(clone / 'runtime.json')
                        inbox = ProtectedInbox(isolated, inbox_holder.path, key_holder.path)
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
                        # The decrypted inbox must be fully inside its held main
                        # file before it is copied to the published destination.
                        with closing(sqlite3.connect(str(inbox_holder.path / 'inbox.sqlite3'),
                                                     isolation_level=None)) as checkpoint:
                            if int(checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]) != 0:
                                raise MemoryFailure('vault_restore_integrity_failed')
                        _authorize_admin(runtime, access)
                        check()
                        # Publish through held handles into a born-private
                        # destination tree; the directory holds block a rename.
                        dest_tree = PrivateTree(destination.parent)
                        with dest_tree as dest_anchor:
                            try:
                                dest_root = dest_anchor.mkdir(destination.name)
                            except MemoryFailure:
                                dest_root = dest_anchor.hold_dir(destination.name)
                            dest_inbox, dest_keys = dest_root.mkdir('inbox'), dest_root.mkdir('keys')
                            for name, holder, filename in (('receiving', dest_keys, 'receiving.json'),
                                                           ('writer', dest_keys, 'writer.key'),
                                                           ('recovery', dest_keys, 'recovery.json'),
                                                           ('inbox', dest_inbox, 'inbox.sqlite3')):
                                src = staged[name]
                                expected = _digest_of(src.stream)
                                try:
                                    target = holder.create_file(filename, write_share=True)
                                    _copy_between(src, target)
                                except MemoryFailure:
                                    target = holder.open_file(filename, write_share=True)
                                    if not hmac.compare_digest(_digest_of(target.stream), expected):
                                        raise MemoryFailure('vault_restore_destination_mismatch') from None
                    finally:
                        for held in staged.values():
                            held.release()
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
