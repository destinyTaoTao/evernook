"""Read-only revalidation of a staged encrypted multi-vault bundle."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import io
import json
import msvcrt
import os
from pathlib import Path
import sqlite3
import struct
import threading
import time
from typing import Any, BinaryIO, Iterator, cast
from uuid import UUID
import zipfile

from nacl.exceptions import BadSignatureError, CryptoError

from .backup import _decrypt_stream, _hex, _json_bytes, _unlock
from .missing_vault import _check_projections_inner, _receipt, read_state
from .models import Access, MemoryFailure
from .paths import external
from .runtime import MemoryRuntime, _json, _unique_object
from .vault_migration import MAX_OBJECTS, _authorize_admin
from .vault_restore import _stopped_service


@contextmanager
def _locked_read(path: Path) -> Iterator[BinaryIO]:
    """Prevent concurrent mutation while parsing an untrusted staged file."""
    before = path.lstat()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise MemoryFailure('vault_workspace_busy')
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise
    with os.fdopen(fd, 'rb') as stream:
        after = os.fstat(stream.fileno())
        import stat
        if (not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or getattr(after, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise MemoryFailure('vault_workspace_unsafe')
        yield stream


def _bound_directory(stream: BinaryIO) -> None:
    """Bound ZIP central-directory work before zipfile reads untrusted records."""
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    if length < 22:
        raise MemoryFailure('invalid_backup')
    stream.seek(length - 22)
    sig, disk, start, on_disk, count, size, offset, comment = struct.unpack('<4s4H2LH', stream.read(22))
    if sig != b'PK\x05\x06' or disk or start or comment or on_disk != count:
        raise MemoryFailure('invalid_backup')
    if size == 0xffffffff or offset == 0xffffffff or count == 0xffff:
        if length < 98:
            raise MemoryFailure('invalid_backup')
        stream.seek(length - 42)
        marker, locator_disk, pos, disks = struct.unpack('<4sLQL', stream.read(20))
        if marker != b'PK\x06\x07' or locator_disk or disks != 1 or pos > length - 98:
            raise MemoryFailure('invalid_backup')
        stream.seek(pos)
        marker64, record_size, _made, _needed, disk64, start64, disk_count, count64, size64, offset64 = struct.unpack('<4sQHHIIQQQQ', stream.read(56))
        if marker64 != b'PK\x06\x06' or record_size != 44 or disk64 or start64 or disk_count != count64:
            raise MemoryFailure('invalid_backup')
        count, size, offset = count64, size64, offset64
    if not 10 <= count <= 67 or size > 65536 or offset + size > length - 22:
        raise MemoryFailure('invalid_backup')
    stream.seek(0)


def check_stage(config: Path, access: Access, bundle: Path, recovery_file: Path, backup_password: bytes, *,
                timeout: float = 120, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Validate signed ciphertext and current link identity, without granting attachment."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_vault_restore_request')
    config, bundle, recovery_file = map(external, (config, bundle, recovery_file))
    deadline = time.monotonic() + timeout
    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_restore_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_restore_timeout')
    def remaining() -> float:
        check()
        return deadline - time.monotonic()
    try:
        with _stopped_service(config.parent):
            runtime = MemoryRuntime(config, service_owner=True)
            _authorize_admin(runtime, access)
            with runtime._connection(ownership_exclusive=True, check=remaining) as core:
                core.execute('BEGIN IMMEDIATE')
                _authorize_admin(runtime, access)
                if not read_state(runtime, core):
                    raise MemoryFailure('protected_recovery_proof_invalid')
                key_id, private, sender = _unlock(recovery_file, backup_password)
                check()
                with _locked_read(bundle) as stream:
                    _bound_directory(stream)
                    with zipfile.ZipFile(stream, 'r') as archive:
                        infos = archive.infolist()
                        if (any(item.compress_type != zipfile.ZIP_STORED for item in infos)
                                or len({item.filename for item in infos}) != len(infos)
                                or any(item.filename in {'backup.json', 'stage-proof.json'} and item.file_size > 65536 for item in infos)):
                            raise MemoryFailure('invalid_backup')
                        header = _json_bytes(archive.read('backup.json'), 65536)
                        stage = _json_bytes(archive.read('stage-proof.json'), 65536)
                        if (set(header) != {'content', 'signature'} or not isinstance(header['content'], dict)
                                or set(stage) != {'content', 'authentication'} or not isinstance(stage['content'], dict)):
                            raise MemoryFailure('invalid_backup')
                        signed, proof = header['content'], stage['content']
                        sender.verify(_json(signed).encode(), _hex(header['signature'], 64))
                        expected_auth = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-stage-v1\0'
                                                    + _json(proof).encode(), hashlib.sha256).hex()
                        if (not isinstance(stage['authentication'], str) or not hmac.compare_digest(stage['authentication'], expected_auth)
                                or set(proof) != {'version', 'backup_id', 'receipt_auth', 'binding', 'projection_digests'} or type(proof['version']) is not int
                                or proof['version'] != 2 or not isinstance(proof['backup_id'], str)
                                or str(UUID(proof['backup_id'])) != proof['backup_id'] or not isinstance(proof['binding'], dict)
                                or not isinstance(proof['projection_digests'], dict)
                                or not isinstance(proof['receipt_auth'], str)
                                or not hmac.compare_digest(proof['receipt_auth'], _receipt(runtime))):
                            raise MemoryFailure('protected_recovery_proof_invalid')
                        if (set(signed) != {'format', 'version', 'backup_key_id', 'status', 'files'}
                                or signed['format'] != 'evernook-protected-multi-backup' or type(signed['version']) is not int
                                or signed['version'] != 1 or signed['backup_key_id'] != key_id
                                or signed['status'] != {'backup_id': proof['backup_id']} or not isinstance(signed['files'], dict)):
                            raise MemoryFailure('invalid_backup')
                        binding = proof['binding']
                        if (set(binding) != {'ledger_id', 'primary', 'created_at', 'inboxes'}
                                or not isinstance(binding['ledger_id'], str) or not isinstance(binding['primary'], str)
                                or not isinstance(binding['created_at'], str)
                                or not isinstance(binding['inboxes'], list) or not 2 <= len(binding['inboxes']) <= 16):
                            raise MemoryFailure('invalid_backup')
                        members: list[dict[str, Any]] = binding['inboxes']
                        for index, item in enumerate(members):
                            if (not isinstance(item, dict) or set(item) != {'slot', 'inbox_id', 'key_id', 'objects', 'link_digest'}
                                    or item['slot'] != f'{index:02d}' or not isinstance(item['inbox_id'], str)
                                    or str(UUID(item['inbox_id'])) != item['inbox_id'] or not isinstance(item['key_id'], str)
                                    or type(item['objects']) is not int or item['objects'] < 0
                                    or not isinstance(item['link_digest'], str) or len(item['link_digest']) != 64
                                    or len(bytes.fromhex(item['link_digest'])) != 32):
                                raise MemoryFailure('invalid_backup')
                        if sum(item['objects'] for item in members) > MAX_OBJECTS:
                            raise MemoryFailure('invalid_backup')
                        expected = {'binding'} | {f'{item["slot"]}-{name}' for item in members
                                                  for name in ('inbox', 'receiving', 'writer', 'recovery')}
                        if (set(signed['files']) != expected or set(archive.namelist()) != {'backup.json', 'stage-proof.json'} | {name + '.enc' for name in expected}
                                or len({item['inbox_id'] for item in members}) != len(members)
                                or len({item['key_id'] for item in members}) != len(members)
                                or binding['primary'] not in {item['inbox_id'] for item in members}
                                or set(proof['projection_digests']) != {item['inbox_id'] for item in members}
                                or any(not isinstance(value, str) or len(value) != 64 for value in proof['projection_digests'].values())):
                            raise MemoryFailure('invalid_backup')
                        # Verify every encrypted member without exposing decrypted vault data.
                        for name in sorted(expected):
                            info = signed['files'][name]
                            maximum = 1073741824 if name.endswith('-inbox') else 65536
                            entry = archive.getinfo(name + '.enc')
                            if (not isinstance(info, dict) or type(info.get('plaintext_size')) is not int
                                    or not 0 <= info['plaintext_size'] <= maximum or entry.file_size > maximum * 2 + 65536):
                                raise MemoryFailure('invalid_backup')
                            digest = hashlib.sha256()
                            with archive.open(entry) as encrypted:
                                while True:
                                    check()
                                    chunk = encrypted.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    digest.update(chunk)
                            if not isinstance(info.get('sha256'), str) or not hmac.compare_digest(digest.hexdigest(), info['sha256']):
                                raise MemoryFailure('backup_integrity_mismatch')
                        # Only non-secret binding metadata is decrypted during preflight.
                        binding_data = io.BytesIO()
                        info = signed['files']['binding']
                        aad = _json({'backup_id': proof['backup_id'], 'backup_key_id': key_id, 'file': 'binding'}).encode()
                        with archive.open('binding.enc') as encrypted_binding:
                            _decrypt_stream(cast(BinaryIO, encrypted_binding), binding_data, private, info, aad, check=check)
                        if _json_bytes(binding_data.getvalue(), 65536) != binding:
                            raise MemoryFailure('protected_recovery_proof_invalid')
                ledger = core.execute('SELECT ledger_id FROM ledger_identity_v1 WHERE id=1').fetchone()
                if ledger is None or ledger['ledger_id'] != binding['ledger_id']:
                    raise MemoryFailure('vault_backup_ledger_mismatch')
                registered = {(row['inbox_id'], row['key_id']) for row in core.execute('SELECT inbox_id,key_id FROM protected_inbox_registry_v1')}
                if registered != {(item['inbox_id'], item['key_id']) for item in members}:
                    raise MemoryFailure('vault_backup_ledger_mismatch')
                core.set_progress_handler(lambda: int(time.monotonic() >= deadline or (cancel is not None and cancel.is_set())), 1000)
                _check_projections_inner(core, require_protected=False)
                grouped: dict[str, list[tuple[str, str]]] = {item['inbox_id']: [] for item in members}
                projections: dict[str, list[tuple[str, str]]] = {item['inbox_id']: [] for item in members}
                has_links = core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_links_v1'").fetchone()
                link_count = int(core.execute('SELECT count(*) FROM protected_links_v1').fetchone()[0]) if has_links else 0
                link_rows = (core.execute('SELECT m.id,m.source,m.event,m.receipt,m.fingerprint,l.reference FROM messages m JOIN protected_links_v1 l ON l.message_id=m.id')
                             if has_links else ())
                for row in link_rows:
                    check()
                    ref = json.loads(row['reference'], object_pairs_hook=_unique_object)
                    if (not isinstance(ref, dict) or set(ref) != {'inbox_id', 'object_id', 'version', 'key_id', 'envelope_sha256'}
                            or ref['inbox_id'] not in grouped or not isinstance(ref['object_id'], str)
                            or type(ref['version']) is not int or ref['version'] != 1
                            or not isinstance(ref['envelope_sha256'], str) or len(ref['envelope_sha256']) != 64):
                        raise MemoryFailure('vault_backup_ledger_mismatch')
                    item = next(value for value in members if value['inbox_id'] == ref['inbox_id'])
                    if ref['key_id'] != item['key_id']:
                        raise MemoryFailure('vault_backup_ledger_mismatch')
                    encoded = _json([ref['object_id'], row['source'], bytes(row['fingerprint']).hex(), ref['envelope_sha256'],
                                     row['receipt'], row['id'], row['reference']]) + '\n'
                    grouped[ref['inbox_id']].append((ref['object_id'], encoded))
                    projections[ref['inbox_id']].append((ref['object_id'], _json([ref['object_id'], row['event']]) + '\n'))
                    if len(grouped[ref['inbox_id']]) > item['objects']:
                        raise MemoryFailure('vault_backup_ledger_mismatch')
                if sum(len(rows) for rows in grouped.values()) != link_count:
                    raise MemoryFailure('vault_backup_ledger_mismatch')
                for item in members:
                    rows = sorted(grouped[item['inbox_id']])
                    ledger_digest = hashlib.sha256(''.join(value for _, value in rows).encode()).hexdigest()
                    projection_digest = hashlib.sha256(''.join(value for _, value in sorted(projections[item['inbox_id']])).encode()).hexdigest()
                    if (len(rows) != item['objects'] or not hmac.compare_digest(ledger_digest, item['link_digest'])
                            or not hmac.compare_digest(projection_digest, proof['projection_digests'][item['inbox_id']])):
                        raise MemoryFailure('vault_backup_ledger_mismatch')
                core.set_progress_handler(None, 0)
                _authorize_admin(runtime, access)
                check()
                core.execute('COMMIT')
                return {'backup_id': proof['backup_id'], 'inboxes': len(members), 'protected_objects': sum(item['objects'] for item in members),
                        'attachment_authorized': False, 'vault_passwords_rechecked': False}
    except (BadSignatureError, CryptoError):
        raise MemoryFailure('backup_authentication_failed') from None
    except MemoryFailure:
        raise
    except sqlite3.Error:
        check()
        raise MemoryFailure('vault_stage_check_failed') from None
    except (OSError, ValueError, TypeError, KeyError, StopIteration, struct.error, zipfile.BadZipFile):
        raise MemoryFailure('vault_stage_check_failed') from None
