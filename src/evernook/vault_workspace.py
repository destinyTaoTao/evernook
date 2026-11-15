"""Authenticated, narrowly scoped recovery workspaces and explicit cleanup."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import msvcrt
import sqlite3
import stat
from typing import Any, Iterator
from uuid import UUID, uuid4

from .backup import _json_bytes
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _atomic_json, _json, _load
from .secure_ntfs import HeldFile, PrivateDirectory, PrivateTree
from .vault_migration import _authorize_admin


_NAME = 'vault-workspaces.json'
_MARKER = 'vault-workspaces-required.json'
_MAX = 128
_PREFIX = {'restore': '.evernook-vault-restore-', 'attach': '.evernook-vault-attach-',
           'multi': '.evernook-vault-multi-', 'candidate': '.evernook-vault-candidate-'}
_FILES = {
    'ledger': {'binding.json', 'memory.sqlite3', 'memory.sqlite3-wal', 'memory.sqlite3-shm',
               'memory.sqlite3-journal', 'runtime.json', 'client.json', 'idempotency.key',
               'memory-service-gate.lock'},
    'vault/inbox': {'inbox.sqlite3', 'inbox.sqlite3-wal', 'inbox.sqlite3-shm', 'inbox.sqlite3-journal'},
    'vault/keys': {'receiving.json', 'writer.key', 'recovery.json'},
}
_DIRS = {'': {'ledger', 'vault'}, 'vault': {'inbox', 'keys'}, 'ledger': set(),
         'vault/inbox': set(), 'vault/keys': set()}
_MULTI_DIRS = {'': {'ledger', 'vaults', 'package'}, 'ledger': set(), 'package': set(),
               'vaults': {f'{i:02d}' for i in range(16)}}
_MULTI_FILES = {'ledger': _FILES['ledger'] | {'stage.bundle'}, 'package': {'backup.json', 'binding.enc'} |
                {f'{i:02d}-{name}.enc' for i in range(16) for name in ('inbox', 'receiving', 'writer', 'recovery')}}
for _slot in sorted(_MULTI_DIRS['vaults']):
    _base = f'vaults/{_slot}'
    _MULTI_DIRS[_base] = {'inbox', 'keys'}
    _MULTI_DIRS[f'{_base}/inbox'] = set()
    _MULTI_DIRS[f'{_base}/keys'] = set()
    _MULTI_FILES[f'{_base}/inbox'] = _FILES['vault/inbox']
    _MULTI_FILES[f'{_base}/keys'] = _FILES['vault/keys']


def _read(runtime: MemoryRuntime) -> list[dict[str, Any]]:
    path, marker = runtime.root / _NAME, runtime.root / _MARKER
    verify_private_directory(runtime.root, extra_files=(path, marker))
    if not path.exists() and not marker.exists():
        return []
    try:
        expected_marker = hmac.digest(runtime._fingerprint_key, b'evernook-vault-workspaces-required-v1', hashlib.sha256).hex()
        if not path.exists() or not marker.exists():
            raise ValueError()
        with marker.open('rb') as file:
            marker_value = _json_bytes(file.read(4097), 4096)
        if (set(marker_value) != {'version', 'authentication'} or type(marker_value['version']) is not int
                or marker_value['version'] != 1 or not isinstance(marker_value['authentication'], str)
                or not hmac.compare_digest(marker_value['authentication'], expected_marker)):
            raise ValueError()
        with path.open('rb') as file:
            value = _json_bytes(file.read(1048577), 1048576)
        if set(value) != {'content', 'authentication'} or not isinstance(value['content'], dict):
            raise ValueError()
        content = value['content']
        if (set(content) != {'version', 'entries'} or type(content['version']) is not int or content['version'] != 1
                or not isinstance(content['entries'], list) or len(content['entries']) > _MAX):
            raise ValueError()
        authentication = hmac.digest(runtime._fingerprint_key, b'evernook-vault-workspaces-v1\0'
                                     + _json(content).encode(), hashlib.sha256).hex()
        if not isinstance(value['authentication'], str) or not hmac.compare_digest(value['authentication'], authentication):
            raise ValueError()
        entries: list[dict[str, Any]] = content['entries']
        seen: set[str] = set()
        for item in entries:
            if (not isinstance(item, dict) or set(item) != {'id', 'kind', 'path', 'device', 'inode'}
                    or item['kind'] not in _PREFIX or not isinstance(item['id'], str)
                    or UUID(item['id']).hex != item['id'] or item['id'] in seen
                    or not isinstance(item['path'], str) or type(item['device']) is not int
                    or type(item['inode']) is not int or item['inode'] <= 0
                    or Path(item['path']).name != _PREFIX[item['kind']] + item['id']):
                raise ValueError()
            seen.add(item['id'])
        return entries
    except (OSError, ValueError, TypeError, KeyError, MemoryFailure):
        raise MemoryFailure('vault_workspace_registry_invalid') from None


def _save(runtime: MemoryRuntime, entries: list[dict[str, Any]]) -> None:
    content = {'version': 1, 'entries': entries}
    authentication = hmac.digest(runtime._fingerprint_key, b'evernook-vault-workspaces-v1\0'
                                 + _json(content).encode(), hashlib.sha256).hex()
    marker = runtime.root / _MARKER
    if not marker.exists():
        required = hmac.digest(runtime._fingerprint_key, b'evernook-vault-workspaces-required-v1', hashlib.sha256).hex()
        _atomic_json(marker, {'version': 1, 'authentication': required})
    _atomic_json(runtime.root / _NAME, {'content': content, 'authentication': authentication})


def _identity(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise MemoryFailure('vault_workspace_unsafe')
    return metadata


def _remove_checked(entry: dict[str, Any]) -> None:
    path = Path(entry['path'])
    dirs, files_by_dir = (_MULTI_DIRS, _MULTI_FILES) if entry['kind'] in ('multi', 'candidate') else (_DIRS, _FILES)
    try:
        path.lstat()
    except FileNotFoundError:
        # The directory may have moved, or deletion may have completed before a crash.
        # Neither proves that the registered contents were destroyed.
        raise MemoryFailure('vault_workspace_missing') from None
    if external(path) != path:
        raise MemoryFailure('vault_workspace_unsafe')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    handles: list[int] = []

    def hold(target: Path, expected: os.stat_result) -> int:
        # No FILE_SHARE_DELETE: the held object cannot be renamed or replaced.
        handle = kernel.CreateFileW(str(target), 0x10000 | 0x80, 0x1 | 0x2, None, 3,
                                    0x02000000 | 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise MemoryFailure('vault_workspace_busy')
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        handles.append(fd)
        actual = os.fstat(fd)
        if (actual.st_dev != expected.st_dev or actual.st_ino != expected.st_ino
                or getattr(actual, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise MemoryFailure('vault_workspace_unsafe')
        return fd

    def inspect(directory: Path, relative: str, expected: os.stat_result) -> None:
        hold(directory, expected)
        children = list(directory.iterdir())
        allowed_dirs, allowed_files = dirs[relative], files_by_dir.get(relative, set())
        files: list[Path] = []
        for child in children:
            info = child.lstat()
            if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise MemoryFailure('vault_workspace_unsafe')
            if stat.S_ISDIR(info.st_mode) and child.name in allowed_dirs:
                inspect(child, f'{relative}/{child.name}'.lstrip('/'), info)
            elif stat.S_ISREG(info.st_mode) and (child.name in allowed_files
                    or (relative == 'ledger' and child.name.startswith(('.provision-', '.pair-')))):
                hold(child, info)
                files.append(child)
            else:
                raise MemoryFailure('vault_workspace_unknown_content')
        verify_private_directory(directory, extra_files=tuple(files))

    try:
        metadata = _identity(path)
        if metadata.st_dev != entry['device'] or metadata.st_ino != entry['inode']:
            raise MemoryFailure('vault_workspace_unsafe')
        inspect(path, '', metadata)
        # Mark the already-open objects, children first; never resolve a deletion target by name.
        while handles:
            fd = handles[-1]
            disposition = ctypes.c_ubyte(1)
            if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4, ctypes.byref(disposition), 1):
                raise MemoryFailure('vault_workspace_cleanup_failed')
            os.close(handles.pop())
    finally:
        while handles:
            os.close(handles.pop())


def _remove(entry: dict[str, Any]) -> None:
    try:
        _remove_checked(entry)
    except (OSError, ValueError, TypeError):
        raise MemoryFailure('vault_workspace_cleanup_failed') from None


def _reserve(runtime: MemoryRuntime, parent: Path, kind: str) -> tuple[Path, dict[str, Any]]:
    if kind not in _PREFIX:
        raise MemoryFailure('invalid_vault_workspace')
    parent = external(parent)
    entries = _read(runtime)
    if len(entries) >= _MAX:
        raise MemoryFailure('vault_workspace_limit')
    ident = uuid4().hex
    path = parent / (_PREFIX[kind] + ident)
    provision_private_directory(path)
    metadata = _identity(path)
    entry = {'id': ident, 'kind': kind, 'path': str(path), 'device': metadata.st_dev, 'inode': metadata.st_ino}
    try:
        _save(runtime, [*entries, entry])
    except BaseException:
        path.rmdir()  # Still empty: no sensitive file was created before registration.
        raise
    return path, entry


def _discard(runtime: MemoryRuntime, core: sqlite3.Connection, entry: dict[str, Any]) -> None:
    # Inspect the caller's transaction, including an uncommitted attachment.
    _guard_active(runtime, core, [entry])
    _remove(entry)
    current = _read(runtime)
    if entry not in current:
        raise MemoryFailure('vault_workspace_registry_invalid')
    _save(runtime, [item for item in current if item != entry])


@contextmanager
def workspace(runtime: MemoryRuntime, parent: Path, kind: str, *, core: sqlite3.Connection) -> Iterator[Path]:
    """Caller holds the stopped-service lock and exclusive ledger gate."""
    if kind == 'candidate':
        raise MemoryFailure('invalid_vault_workspace')
    path, entry = _reserve(runtime, parent, kind)
    try:
        yield path
    finally:
        _discard(runtime, core, entry)


@contextmanager
def candidate_workspace(runtime: MemoryRuntime, parent: Path) -> Iterator[Path]:
    """Always retain registration, including on error; caller holds service and ledger locks."""
    path, _entry = _reserve(runtime, parent, 'candidate')
    yield path


@contextmanager
def secure_workspace(runtime: MemoryRuntime, parent: Path, kind: str, *,
                     core: sqlite3.Connection) -> Iterator[PrivateDirectory]:
    """Handle-held workspace: directories and files are born owner-only.

    The tree is anchored without following reparse points and every created
    object receives the caller-only security descriptor at creation, so no
    window exists between existence and privacy. Registration records the
    handle-derived identity before any content exists; cleanup reuses the
    authenticated registry path so crash remnants stay recoverable. Caller
    holds the stopped-service lock and exclusive ledger gate.
    """
    if kind not in _PREFIX:
        raise MemoryFailure('invalid_vault_workspace')
    parent = external(parent)
    entries = _read(runtime)
    if len(entries) >= _MAX:
        raise MemoryFailure('vault_workspace_limit')
    ident = uuid4().hex
    path = parent / (_PREFIX[kind] + ident)
    tree = PrivateTree(parent)
    entry: dict[str, Any] | None = None
    registered = False
    try:
        with tree as anchored:
            try:
                root_dir = anchored.mkdir(path.name)
                metadata = root_dir.identity()
                entry = {'id': ident, 'kind': kind, 'path': str(path),
                         'device': metadata.st_dev, 'inode': metadata.st_ino}
                _save(runtime, [*entries, entry])
                registered = True
                yield root_dir
            except BaseException:
                if not registered:
                    # Registration never completed: remove what this tree
                    # created while the holds are still open.
                    tree.delete_created()
                raise
    finally:
        if registered and entry is not None:
            _discard(runtime, core, entry)


def _guard_active(runtime: MemoryRuntime, core: sqlite3.Connection, selected: list[dict[str, Any]]) -> None:
    """Refuse all deletion when active identity cannot be safely excluded."""
    from .missing_vault import attachment, multi_attachment, read_state
    missing = read_state(runtime, core)
    single, multi = attachment(runtime, core), multi_attachment(runtime, core)
    config = _load(runtime.root / 'runtime.json')
    active: list[Path] = []
    if single is not None:
        active.extend((Path(single['inbox_dir']), Path(single['key_dir'])))
    binding = multi['binding'] if multi is not None else None
    if binding is None and config.get('protected_multi_binding') is True:
        from .vault_multi_binding import read as read_binding
        binding = read_binding(runtime)
        if binding is None:
            raise MemoryFailure('protected_multi_binding_mismatch')
    if binding is not None:
        bound = binding.get('inboxes')
        if not isinstance(bound, list) or any(not isinstance(item, dict)
                or not isinstance(item.get('inbox_dir'), str) or not isinstance(item.get('key_dir'), str)
                for item in bound):
            raise MemoryFailure('protected_recovery_proof_invalid')
        for item in bound:
            active.extend((Path(item['inbox_dir']), Path(item['key_dir'])))
    single_bound = None
    if single is None and binding is None and config.get('protected_single_binding') is True:
        from .vault_single_binding import read as read_single_binding
        single_bound = read_single_binding(runtime)
        if single_bound is None:
            raise MemoryFailure('protected_single_binding_mismatch')
        active.extend((Path(single_bound['inbox_dir']), Path(single_bound['key_dir'])))
    if not missing and single is None and binding is None and single_bound is None:
        # Legacy and unactivated runtimes did not persist active paths.  Even
        # an empty registry cannot prove no unregistered inbox is in use.
        raise MemoryFailure('vault_workspace_unbound_active')
    if any(any(path.is_relative_to(Path(entry['path'])) or Path(entry['path']).is_relative_to(path)
               for path in active) for entry in selected):
        raise MemoryFailure('vault_workspace_active')


def list_workspaces(config: Path, access: Access) -> list[dict[str, Any]]:
    from .vault_restore import _stopped_service
    config = external(config)
    with _stopped_service(config.parent):
        runtime = MemoryRuntime(config, service_owner=True)
        with runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            _authorize_admin(runtime, access)
            entries = _read(runtime)
            core.execute('COMMIT')
            return entries


def cleanup_workspaces(config: Path, access: Access, ids: list[str]) -> dict[str, object]:
    """Explicit cleanup of selected authenticated workspaces, never prefix discovery."""
    from .vault_restore import _stopped_service
    if not isinstance(ids, list) or not ids or len(ids) > _MAX or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise MemoryFailure('invalid_vault_workspace_request')
    config = external(config)
    with _stopped_service(config.parent):
        runtime = MemoryRuntime(config, service_owner=True)
        with runtime._connection(ownership_exclusive=True) as core:
            core.execute('BEGIN IMMEDIATE')
            _authorize_admin(runtime, access)
            entries = _read(runtime)
            selected = [item for item in entries if item['id'] in ids]
            if len(selected) != len(ids):
                raise MemoryFailure('vault_workspace_not_registered')
            _guard_active(runtime, core, selected)
            for entry in selected:
                _authorize_admin(runtime, access)
                _remove(entry)
                entries.remove(entry)
                _save(runtime, entries)
            core.execute('COMMIT')
            return {'removed': len(selected), 'remaining': len(entries), 'physical_erasure_verified': False}
