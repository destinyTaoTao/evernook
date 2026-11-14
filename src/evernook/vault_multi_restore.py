"""Offline, all-or-nothing validation and staging of a multi-inbox backup."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
import io
from itertools import islice
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable
from uuid import UUID
import zipfile

from nacl.exceptions import BadSignatureError, CryptoError

from .backup import _decrypt, _hex, _json_bytes, _unlock
from .missing_vault import read_state
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_bytes, _atomic_json, _json
from .vault_migration import _authorize_admin
from .vault_multi_backup import _link_digest
from .vault_multi_verify import _inspect
from .vault_restore import _stopped_service
from .vault_stage_publication import publish_guard
from .vault_workspace import workspace


@dataclass(frozen=True)
class MultiVaultRestoreStage:
    backup_id: str
    inboxes: int
    protected_objects: int
    destination: str
    attached: bool = False
    daily_admitted: bool = False


def restore_stage(config: Path, access: Access, source: Path, destination: Path, recovery_file: Path,
                  backup_password: bytes, password_for_inbox: Callable[[str, str], bytes], *,
                  cancel: threading.Event | None = None, timeout: float = 120) -> MultiVaultRestoreStage:
    """Validate the complete set; publication does not attach or clear missing-vault state."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120 or not callable(password_for_inbox):
        raise MemoryFailure('invalid_vault_restore_request')
    config, source, destination, recovery_file = map(external, (config, source, destination, recovery_file))
    root = config.parent
    if any(destination.is_relative_to(path) or path.is_relative_to(destination)
           for path in (root, source, recovery_file.parent)):
        raise MemoryFailure('unsafe_backup_path')
    if destination.exists():
        raise MemoryFailure('backup_destination_exists')
    destination_parent_identity = destination.parent.stat()
    deadline = time.monotonic() + timeout
    publication_started = False
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_restore_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_restore_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()
    try:
        check()
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
                children = list(islice(source.iterdir(), 67))
                if len(children) > 66:
                    raise MemoryFailure('invalid_backup')
                verify_private_directory(source, extra_files=tuple(children))
                with (source / 'backup.json').open('rb') as stream:
                    header_raw = stream.read(65537)
                    header = _json_bytes(header_raw, 65536)
                if set(header) != {'content', 'signature'} or not isinstance(header['content'], dict):
                    raise MemoryFailure('invalid_backup')
                content = header['content']
                sender.verify(_json(content).encode(), _hex(header['signature'], 64))
                if (set(content) != {'format', 'version', 'backup_key_id', 'status', 'files'}
                        or content['format'] != 'evernook-protected-multi-backup'
                        or type(content['version']) is not int or content['version'] != 1
                        or content['backup_key_id'] != key_id or not isinstance(content['status'], dict)
                        or set(content['status']) != {'backup_id'} or not isinstance(content['files'], dict)):
                    raise MemoryFailure('invalid_backup')
                backup_id = content['status']['backup_id']
                if not isinstance(backup_id, str) or str(UUID(backup_id)) != backup_id:
                    raise MemoryFailure('invalid_backup')
                files: dict[str, Any] = content['files']
                count = (len(files) - 1) // 4
                expected = {'binding'} | {f'{i:02d}-{name}' for i in range(count)
                                         for name in ('inbox', 'receiving', 'writer', 'recovery')}
                if not 2 <= count <= 16 or set(files) != expected:
                    raise MemoryFailure('invalid_backup')
                names = {'backup.json'} | {name + '.enc' for name in expected}
                if {item.name for item in children} != names:
                    raise MemoryFailure('invalid_backup')
                def decrypt(name: str, output: Any) -> None:
                    check()
                    info = files[name]
                    maximum = 1073741824 if name.endswith('-inbox') else 65536
                    if (not isinstance(info, dict) or type(info.get('plaintext_size')) is not int
                            or not 0 <= info['plaintext_size'] <= maximum
                            or (source / (name + '.enc')).stat().st_size > maximum * 2 + 65536):
                        raise MemoryFailure('invalid_backup')
                    aad = _json({'backup_id': backup_id, 'backup_key_id': key_id, 'file': name}).encode()
                    _decrypt(source / (name + '.enc'), output, private, info, aad, check=check)
                raw_binding = io.BytesIO()
                decrypt('binding', raw_binding)
                binding = _json_bytes(raw_binding.getvalue(), 65536)
                if (set(binding) != {'ledger_id', 'primary', 'created_at', 'inboxes'}
                        or not isinstance(binding['ledger_id'], str) or not isinstance(binding['primary'], str)
                        or not isinstance(binding['created_at'], str) or not isinstance(binding['inboxes'], list)
                        or len(binding['inboxes']) != count):
                    raise MemoryFailure('invalid_backup')
                members: list[dict[str, Any]] = binding['inboxes']
                for index, member in enumerate(members):
                    if (not isinstance(member, dict) or set(member) != {'slot', 'inbox_id', 'key_id', 'objects', 'link_digest'}
                            or member['slot'] != f'{index:02d}' or not isinstance(member['inbox_id'], str)
                            or str(UUID(member['inbox_id'])) != member['inbox_id'] or not isinstance(member['key_id'], str)
                            or type(member['objects']) is not int or member['objects'] < 0
                            or not isinstance(member['link_digest'], str) or len(member['link_digest']) != 64
                            or len(bytes.fromhex(member['link_digest'])) != 32):
                        raise MemoryFailure('invalid_backup')
                if (len({item['inbox_id'] for item in members}) != count
                        or len({item['key_id'] for item in members}) != count
                        or binding['primary'] not in {item['inbox_id'] for item in members}):
                    raise MemoryFailure('invalid_backup')
                with workspace(runtime, destination.parent, 'multi') as temporary:
                    vaults, clone = temporary / 'vaults', temporary / 'ledger'
                    provision_private_directory(vaults)
                    provision_private_directory(clone)
                    for member in members:
                        slot = member['slot']
                        slot_dir = vaults / slot
                        provision_private_directory(slot_dir)
                        inbox_dir, key_dir = slot_dir / 'inbox', slot_dir / 'keys'
                        provision_private_directory(inbox_dir)
                        provision_private_directory(key_dir)
                        for name, target in (('inbox', inbox_dir / 'inbox.sqlite3'), ('receiving', key_dir / 'receiving.json'),
                                             ('writer', key_dir / 'writer.key'), ('recovery', key_dir / 'recovery.json')):
                            with target.open('xb') as output:
                                decrypt(f'{slot}-{name}', output)
                                output.flush()
                                os.fsync(output.fileno())
                    with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as source_db:
                        with closing(sqlite3.connect(clone / 'memory.sqlite3')) as copied:
                            source_db.backup(copied, pages=256, progress=lambda *_: check(), sleep=.05)
                            copied.execute('DROP TABLE IF EXISTS restored_missing_vault_v1')
                            copied.execute('DROP TABLE IF EXISTS restored_vault_attachment_v1')
                            copied.commit()
                    _atomic_json(clone / 'runtime.json', {'schema_version': 1, 'data_dir': str(clone), 'busy_timeout_ms': 5000})
                    shutil.copyfile(root / 'client.json', clone / 'client.json')
                    _atomic_bytes(clone / 'idempotency.key', runtime._fingerprint_key)
                    isolated = MemoryRuntime(clone / 'runtime.json')
                    declared = []
                    for member in members:
                        inbox_file = vaults / member['slot'] / 'inbox' / 'inbox.sqlite3'
                        with closing(sqlite3.connect(inbox_file.as_uri() + '?mode=ro', uri=True)) as integrity:
                            integrity.set_progress_handler(lambda: int(time.monotonic() >= deadline
                                or (cancel is not None and cancel.is_set())), 1000)
                            try:
                                if integrity.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                                    raise MemoryFailure('vault_restore_integrity_failed')
                            except sqlite3.OperationalError:
                                check()
                                raise
                        inbox = ProtectedInbox(isolated, vaults / member['slot'] / 'inbox', vaults / member['slot'] / 'keys')
                        if inbox.inbox_id != member['inbox_id'] or inbox.public['key_id'] != member['key_id']:
                            raise MemoryFailure('vault_backup_ledger_mismatch')
                        password = password_for_inbox(member['slot'], member['inbox_id'])
                        declared.append((inbox, password))
                    def verify(snapshot: dict[str, Any], copied_core: sqlite3.Connection, stores: list[Any]) -> None:
                        if snapshot['ledger_id'] != binding['ledger_id']:
                            raise MemoryFailure('vault_backup_ledger_mismatch')
                        for inbox, _private, store, _material in stores:
                            member = next(item for item in members if item['inbox_id'] == inbox.inbox_id)
                            summary = next(item for item in snapshot['inboxes'] if item['inbox_id'] == inbox.inbox_id)
                            if (summary['objects'] != member['objects']
                                    or not hmac.compare_digest(_link_digest(copied_core, store, check), member['link_digest'])):
                                raise MemoryFailure('vault_backup_ledger_mismatch')
                    _inspect(isolated, access, tuple(declared), decrypt=True, require_complete=True,
                             require_readable=False, locked_stores=verify, deadline_limit=deadline,
                             check_extra=check, cancel=cancel)
                    stage_content = {'version': 1, 'backup_id': backup_id, 'binding': binding}
                    authentication = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-stage-v1\0'
                                                 + _json(stage_content).encode(), hashlib.sha256).hex()
                    proof_bytes = _json({'content': stage_content, 'authentication': authentication}).encode()
                    bundle = clone / 'stage.bundle'
                    with bundle.open('xb') as output:
                        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                            archive.writestr('backup.json', header_raw)
                            archive.writestr('stage-proof.json', proof_bytes)
                            for name in sorted(expected):
                                check()
                                digest = hashlib.sha256()
                                with (source / (name + '.enc')).open('rb') as encrypted_source, archive.open(name + '.enc', 'w', force_zip64=True) as target:
                                    while True:
                                        check()
                                        chunk = encrypted_source.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        digest.update(chunk)
                                        target.write(chunk)
                                if not hmac.compare_digest(digest.hexdigest(), files[name]['sha256']):
                                    raise MemoryFailure('backup_integrity_mismatch')
                        output.flush()
                        os.fsync(output.fileno())
                        bundle_identity = os.fstat(output.fileno())
                    _authorize_admin(runtime, access)
                    check()
                    with publish_guard(bundle, bundle_identity, destination.parent, destination_parent_identity, check) as (locked_stream, publish):
                        with zipfile.ZipFile(locked_stream, 'r') as archive:
                            names_in_bundle = archive.namelist()
                            if (len(names_in_bundle) != len(names) + 1 or set(names_in_bundle) != names | {'stage-proof.json'}
                                    or any(item.compress_type != zipfile.ZIP_STORED for item in archive.infolist())
                                    or archive.getinfo('backup.json').file_size != len(header_raw)
                                    or archive.getinfo('stage-proof.json').file_size != len(proof_bytes)
                                    or archive.read('backup.json') != header_raw or archive.read('stage-proof.json') != proof_bytes):
                                raise MemoryFailure('vault_stage_changed')
                            for name in sorted(expected):
                                digest = hashlib.sha256()
                                with archive.open(name + '.enc') as bundled_source:
                                    while True:
                                        check()
                                        chunk = bundled_source.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        digest.update(chunk)
                                if not hmac.compare_digest(digest.hexdigest(), files[name]['sha256']):
                                    raise MemoryFailure('vault_stage_changed')
                        _authorize_admin(runtime, access)
                        publication_started = True
                        publish(destination)
                core.execute('COMMIT')
                return MultiVaultRestoreStage(backup_id, count, sum(item['objects'] for item in members), str(destination))
    except (BadSignatureError, CryptoError):
        raise MemoryFailure('backup_authentication_failed') from None
    except MemoryFailure:
        if publication_started:
            raise MemoryFailure('vault_restore_outcome_unknown') from None
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, StopIteration, zipfile.BadZipFile):
        if publication_started:
            raise MemoryFailure('vault_restore_outcome_unknown') from None
        raise MemoryFailure('vault_restore_failed') from None
