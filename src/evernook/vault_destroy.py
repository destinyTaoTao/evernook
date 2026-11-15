"""Trusted local emergency destruction of one bound vault (#116 / #218).

Authorization is the vault password itself: every first run re-unlocks the
live recovery material, so destruction is impossible without the secret the
user holds.  The vault must be a durably bound member of the service (single
binding, multi binding or an authenticated restored attachment) — unbound
legacy or unknown paths are refused.  Destruction removes the local
ciphertext and key material with identity-checked held-handle unlink
operations, records HMAC'd crash-safe progress in the runtime root, cleans
every registered recovery workspace, and leaves the ordinary ledger and any
independent backups untouched.  Filesystem unlink is not physical erasure;
residual undelete risk and external copies are documented boundaries.
"""
from __future__ import annotations

from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import msvcrt
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from typing import Any

from .backup import _json_bytes
from .missing_vault import attachment, multi_attachment
from .models import Access, MemoryFailure
from .paths import external, verify_private_directory
from nacl.public import PublicKey

from .protected import _public
from .runtime import MemoryRuntime, _atomic_bytes, _atomic_json, _json, _load, _unique_object
from .vault_crypto import _UnlockTarget, _unlock
from .vault_migration import _authorize_admin
from .vault_restore import _stopped_service
from .vault_workspace import _read as _read_workspaces, _remove, _save as _save_workspaces


class _LiveVault:
    """Concrete unlock target for a live bound vault under the held gate."""

    def __init__(self, key_root: Path, public: dict[str, Any], recipient: PublicKey) -> None:
        self.key_root, self.public, self.recipient = key_root, public, recipient



INBOX_FILES = ('inbox.sqlite3', 'inbox.sqlite3-wal', 'inbox.sqlite3-shm', 'inbox.sqlite3-journal')
KEY_FILES = ('receiving.json', 'writer.key', 'recovery.json')
_STEPS = ('verify_bound', 'verify_password', 'freeze_service', 'delete_inbox', 'delete_keys',
          'remove_directories', 'clean_workspaces', 'release_binding')


def _record_path(runtime: MemoryRuntime, inbox_id: str) -> Path:
    return runtime.root / f'vault-destruction-{inbox_id}.json'


def _load_record(runtime: MemoryRuntime, inbox_id: str) -> dict[str, Any] | None:
    path = _record_path(runtime, inbox_id)
    verify_private_directory(runtime.root, extra_files=(path,))
    if not path.exists():
        return None
    try:
        value = _json_bytes(path.read_bytes()[:65537], 65536)
        content = value['content']
        expected = hmac.digest(runtime._fingerprint_key, b'evernook-vault-destruction-v1\0'
                               + _json(content).encode(), hashlib.sha256).hex()
        if (set(value) != {'content', 'authentication'} or not isinstance(content, dict)
                or set(content) != {'version', 'inbox_id', 'steps'} or type(content['version']) is not int
                or content['version'] != 1 or content['inbox_id'] != inbox_id
                or not isinstance(content['steps'], dict)
                or any(key not in _STEPS or state not in ('pending', 'done')
                       for key, state in content['steps'].items())
                or not hmac.compare_digest(value['authentication'], expected)):
            raise ValueError()
        return content
    except (OSError, ValueError, TypeError, KeyError):
        raise MemoryFailure('vault_destruction_record_invalid') from None


def _save_record(runtime: MemoryRuntime, content: dict[str, Any]) -> None:
    authentication = hmac.digest(runtime._fingerprint_key, b'evernook-vault-destruction-v1\0'
                                 + _json(content).encode(), hashlib.sha256).hex()
    _atomic_bytes(_record_path(runtime, str(content['inbox_id'])),
                  _json({'content': content, 'authentication': authentication}).encode())


def _hold_and_check(path: Path) -> tuple[Any, int]:
    """Open one deletion target without delete sharing and verify its identity."""
    before = path.lstat()
    if getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise MemoryFailure('vault_destroy_unsafe')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x10000 | 0x80, 0x1 | 0x2, None, 3, 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise MemoryFailure('vault_destroy_busy')
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise
    actual = os.fstat(fd)
    if ((actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
            or getattr(actual, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        os.close(fd)
        raise MemoryFailure('vault_destroy_unsafe')
    return kernel, fd


def _unlink_held(kernel: Any, fd: int) -> None:
    disposition = ctypes.c_ubyte(1)
    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4, ctypes.byref(disposition), 1):
        os.close(fd)
        raise MemoryFailure('vault_destroy_failed')
    os.close(fd)


def _delete_contents(directory: Path, allowed: tuple[str, ...]) -> None:
    """Unlink exactly the expected children; tolerate already-deleted files."""
    if not directory.exists():
        return
    verify_private_directory(directory)
    present = sorted(child.name for child in directory.iterdir())
    if any(name not in allowed for name in present):
        raise MemoryFailure('vault_destroy_unknown_content')
    for name in present:
        kernel, fd = _hold_and_check(directory / name)
        _unlink_held(kernel, fd)
    if directory.exists():
        directory.rmdir()


def _resolve_bound_target(runtime: MemoryRuntime, core: sqlite3.Connection,
                          inbox_dir: Path, key_dir: Path) -> str:
    """Return the inbox id of the durably bound vault at these exact paths."""
    from .vault_multi_binding import read as read_multi
    from .vault_single_binding import read as read_single
    config = _load(runtime.root / 'runtime.json')
    if multi_attachment(runtime, core) is not None or config.get('protected_multi_binding') is True:
        binding = read_multi(runtime)
        if binding is None:
            raise MemoryFailure('protected_multi_binding_mismatch')
        matches = [item for item in binding['inboxes']
                   if isinstance(item, dict) and item.get('inbox_dir') == str(inbox_dir)
                   and item.get('key_dir') == str(key_dir)]
        if len(matches) != 1:
            raise MemoryFailure('vault_destroy_unbound')
        return str(matches[0]['inbox_id'])
    if config.get('protected_single_binding') is True:
        bound = read_single(runtime)
        if bound is None or bound['inbox_dir'] != str(inbox_dir) or bound['key_dir'] != str(key_dir):
            raise MemoryFailure('vault_destroy_unbound')
        return str(bound['inbox_id'])
    restored = attachment(runtime, core)
    if restored is None or restored['inbox_dir'] != str(inbox_dir) or restored['key_dir'] != str(key_dir):
        raise MemoryFailure('vault_destroy_unbound')
    return str(restored['inbox_id'])


def _release_binding(runtime: MemoryRuntime, core: sqlite3.Connection, inbox_id: str) -> None:
    """Keep the binding invariant: fewer vaults degrade, none returns to local.

    A multi set that still holds at least two members keeps its authenticated
    binding (primary is re-derived if it was destroyed); one remaining member
    degrades to the single binding; no remaining member clears the protected
    flags so ordinary memory stays readable in local mode again.
    """
    from .vault_multi_binding import BINDING_NAME as MULTI_NAME, _auth as multi_auth, read as read_multi
    from .vault_single_binding import BINDING_NAME as SINGLE_NAME, _auth as single_auth
    config = _load(runtime.root / 'runtime.json')
    if config.get('protected_multi_binding') is True:
        binding = read_multi(runtime)
        if binding is None:
            raise MemoryFailure('protected_multi_binding_mismatch')
        remaining = [item for item in binding['inboxes'] if item.get('inbox_id') != inbox_id]
        has_marker = core.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='restored_multi_attachment_v1'").fetchone()
        marker = multi_attachment(runtime, core) if has_marker else None
        if len(remaining) >= 2:
            if binding['primary'] == inbox_id:
                binding = {**binding, 'inboxes': remaining, 'primary': remaining[0]['inbox_id']}
            else:
                binding = {**binding, 'inboxes': remaining}
            _atomic_json(runtime.root / MULTI_NAME,
                         {'content': binding, 'authentication': multi_auth(runtime, binding)})
            if marker is not None:
                content = {'version': 1, 'receipt_auth': marker['receipt_auth'], 'binding': binding}
                authentication = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-attachment-v1\0'
                                             + _json(content).encode(), hashlib.sha256).hex()
                core.execute('UPDATE restored_multi_attachment_v1 SET content=?, authentication=? WHERE id=1',
                             (_json(content), authentication))
        else:
            (runtime.root / MULTI_NAME).unlink()
            if marker is not None:
                core.execute('DROP TABLE restored_multi_attachment_v1')
            if len(remaining) == 1:
                entry = remaining[0]
                single = {'version': 1, 'inbox_id': entry['inbox_id'], 'key_id': entry['key_id'],
                          'inbox_dir': entry['inbox_dir'], 'key_dir': entry['key_dir'],
                          'material': entry['material']}
                _atomic_json(runtime.root / SINGLE_NAME,
                             {'content': single, 'authentication': single_auth(runtime, single)})
                degraded = {key: value for key, value in config.items() if key != 'protected_multi_binding'}
                degraded['protected_single_binding'] = True
                _atomic_json(runtime.root / 'runtime.json', degraded)
                return
            _atomic_json(runtime.root / 'runtime.json',
                         {'schema_version': 1, 'data_dir': config['data_dir'],
                          'busy_timeout_ms': config['busy_timeout_ms']})
        return
    if config.get('protected_single_binding') is True:
        (runtime.root / SINGLE_NAME).unlink()
        _atomic_json(runtime.root / 'runtime.json',
                     {'schema_version': 1, 'data_dir': config['data_dir'],
                      'busy_timeout_ms': config['busy_timeout_ms']})
        return
    restored = attachment(runtime, core)
    if restored is not None and restored['inbox_id'] == inbox_id:
        core.execute('DROP TABLE restored_vault_attachment_v1')
        _atomic_json(runtime.root / 'runtime.json',
                     {'schema_version': 1, 'data_dir': config['data_dir'],
                      'busy_timeout_ms': config['busy_timeout_ms']})


def destroy_vault(config: Path, access: Access, inbox_dir: Path, key_dir: Path, password: bytes, *,
                  cancel: threading.Event | None = None, timeout: float = 120) -> dict[str, Any]:
    """Destroy one bound vault's local materials; ordinary memory stays readable."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120 or not isinstance(password, bytes):
        raise MemoryFailure('invalid_vault_destroy_request')
    config, inbox_dir, key_dir = map(external, (config, inbox_dir, key_dir))
    root = config.parent
    if (inbox_dir.is_relative_to(root) or root.is_relative_to(inbox_dir)
            or key_dir.is_relative_to(root) or root.is_relative_to(key_dir)
            or inbox_dir.is_relative_to(key_dir) or key_dir.is_relative_to(inbox_dir)):
        raise MemoryFailure('unsafe_backup_path')
    deadline = time.monotonic() + timeout

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_destroy_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_destroy_timeout')

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
                inbox_id = _resolve_bound_target(runtime, core, inbox_dir, key_dir)
                record = _load_record(runtime, inbox_id)
                authorized = record is not None and record['steps'].get('verify_password') == 'done'
                if not authorized:
                    # First authorization: the live vault must still exist and
                    # unlock with the password the user holds.  The exclusive
                    # gate is already held, so the checks run on raw handles
                    # instead of a nested reader gate.
                    public, recipient, _sender = _public(key_dir / 'receiving.json')
                    with closing(sqlite3.connect(str(inbox_dir / 'inbox.sqlite3'), timeout=1,
                                                 isolation_level=None)) as db:
                        db.row_factory = sqlite3.Row
                        settings = db.execute('SELECT * FROM settings WHERE singleton=1').fetchone()
                        if settings is None:
                            raise MemoryFailure('protected_binding_mismatch')
                        binding_key = hmac.digest(runtime._fingerprint_key,
                                                  b'evernook-protected-inbox-v1', hashlib.sha256)
                        try:
                            stored_public = json.loads(settings['public'], object_pairs_hook=_unique_object)
                        except ValueError:
                            raise MemoryFailure('protected_binding_mismatch') from None
                        if (settings['inbox_id'] != inbox_id or stored_public != public
                                or not hmac.compare_digest(bytes(settings['runtime_binding']), binding_key)):
                            raise MemoryFailure('protected_attachment_mismatch')
                    _unlock(_LiveVault(key_dir, public, recipient), password)
                if record is None:
                    record = {'version': 1, 'inbox_id': inbox_id,
                              'steps': {step: 'pending' for step in _STEPS}}
                if record['inbox_id'] != inbox_id or set(record['steps']) != set(_STEPS):
                    raise MemoryFailure('vault_destruction_record_invalid')

                def mark(step: str) -> None:
                    record['steps'][step] = 'done'
                    _save_record(runtime, record)

                mark('verify_bound')
                mark('verify_password')
                # The stopped-service lock is held: writes and decryption are
                # frozen for the whole critical section and stay frozen after
                # the material is gone.
                mark('freeze_service')
                check()
                _delete_contents(inbox_dir, INBOX_FILES)
                mark('delete_inbox')
                check()
                _delete_contents(key_dir, KEY_FILES)
                mark('delete_keys')
                mark('remove_directories')
                check()
                # Every registered recovery workspace holds key or ciphertext
                # copies and must not survive the destruction of its origin.
                entries = _read_workspaces(runtime)
                removed = 0
                for entry in entries:
                    _authorize_admin(runtime, access)
                    _remove(entry)
                    entries.remove(entry)
                    _save_workspaces(runtime, entries)
                    removed += 1
                mark('clean_workspaces')
                check()
                _release_binding(runtime, core, inbox_id)
                mark('release_binding')
                core.execute('COMMIT')
                return {'inbox_id': inbox_id, 'steps': record['steps'], 'workspaces_removed': removed,
                        'physical_erasure_verified': False,
                        'independent_backups_scope': 'outside_local_destruction'}
    except MemoryFailure:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise MemoryFailure('vault_destroy_failed') from None
