"""Handle-relative, birth-private NTFS creation for managed vault workflows.

Holds prevent object replacement and selected access classes, not every
same-account mutation. In particular, a directory share mask does not block
creating files by a child path. Relative FILE_CREATE refuses planted names;
callers must keep file identities held and validate complete publication.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import sys
from typing import BinaryIO, Iterator

if sys.platform == 'win32':
    import msvcrt

from .models import MemoryFailure
from .paths import external


class _UnicodeString(ctypes.Structure):
    _fields_ = [('length', wintypes.USHORT), ('maximum', wintypes.USHORT), ('buffer', wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.ULONG), ('root', wintypes.HANDLE), ('name', ctypes.POINTER(_UnicodeString)),
                ('attributes', wintypes.ULONG), ('security', wintypes.LPVOID), ('quality', wintypes.LPVOID)]


class _IoStatus(ctypes.Structure):
    _fields_ = [('status', ctypes.c_void_p), ('information', ctypes.c_size_t)]


def _name(value: str) -> None:
    try:
        invalid = (not isinstance(value, str) or not value or value in ('.', '..') or value[-1] in (' ', '.')
                   or any(character in '\\/:' or ord(character) < 32 for character in value)
                   or len(value.encode('utf-16-le')) > 510
                   or (hasattr(os.path, 'isreserved') and os.path.isreserved(value)))
    except UnicodeError:
        invalid = True
    if invalid:
        raise MemoryFailure('secure_creation_invalid_name')


class _Native:
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        self.ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
        self.kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
        self.kernel.LocalFree.restype = wintypes.HLOCAL
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        self.kernel.GetDriveTypeW.restype = wintypes.UINT
        self.kernel.GetVolumeInformationByHandleW.argtypes = (
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD)
        self.kernel.GetVolumeInformationByHandleW.restype = wintypes.BOOL
        self.kernel.DuplicateHandle.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                                 ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.DuplicateHandle.restype = wintypes.BOOL
        self.kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        self.kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG))
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG))
        self.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self.advapi.GetSecurityInfo.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID))
        self.advapi.GetSecurityInfo.restype = wintypes.DWORD
        self.ntdll.NtCreateFile.argtypes = (ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
            ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatus), wintypes.LPVOID,
            wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG)
        self.ntdll.NtCreateFile.restype = ctypes.c_long

    def close(self, handle: int) -> None:
        self.kernel.CloseHandle(wintypes.HANDLE(handle))

    def identity(self, handle: int) -> os.stat_result:
        duplicate = wintypes.HANDLE()
        process = self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2) or duplicate.value is None:
            raise MemoryFailure('secure_creation_refused')
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY)
        except BaseException:
            self.close(duplicate.value)
            raise
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def sddl(self, descriptor: int) -> str:
        result = wintypes.LPWSTR()
        if not self.advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(result), None):
            raise MemoryFailure('secure_creation_acl_invalid')
        try:
            if result.value is None:
                raise MemoryFailure('secure_creation_acl_invalid')
            return result.value
        finally:
            self.kernel.LocalFree(ctypes.cast(result, wintypes.HLOCAL))

    def verify_acl(self, handle: int, expected: str) -> None:
        descriptor = wintypes.LPVOID()
        error = self.advapi.GetSecurityInfo(handle, 1, 4, None, None, None, None, ctypes.byref(descriptor))
        if error or descriptor.value is None:
            if descriptor.value is not None:
                self.kernel.LocalFree(descriptor)
            raise MemoryFailure('secure_creation_acl_invalid')
        try:
            if self.sddl(descriptor.value) != expected:
                raise MemoryFailure('secure_creation_acl_invalid')
        finally:
            self.kernel.LocalFree(descriptor)

    def relative(self, parent: int, name: str, *, create: bool, directory: bool, descriptor: int | None = None,
                 share: int | None = None, access: int | None = None) -> int:
        _name(name)
        encoded = name.encode('utf-16-le')
        buffer = ctypes.create_unicode_buffer(name)
        value = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE; RootDirectory is the held handle.
        attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(value),
                                       0x1040, descriptor, None)
        io = _IoStatus()
        result = wintypes.HANDLE()
        # Existing ancestors only need traverse/metadata. New objects also need
        # write permissions and are born with the caller-only descriptor. File
        # holds never carry DELETE access: Python and SQLite reopen files by
        # path without FILE_SHARE_DELETE and a DELETE hold would block them.
        desired = access if access is not None else (0x001700a7 if directory and create else 0x001200a1 if directory
                                                     else 0xc0120083)
        # Deny directory data/delete opens and file data writes. Child-path
        # creation and write-attributes opens are separate Windows semantics.
        default_share = 0x1 if (directory and create) else (0x3 if directory else 0x1)
        share = default_share if share is None else share
        # FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT and the expected object type.
        options = (0x00200021 if directory else 0x00200060)
        status = self.ntdll.NtCreateFile(ctypes.byref(result), desired, ctypes.byref(attributes), ctypes.byref(io),
                                         None, 0x10 if directory else 0x80, share, 2 if create else 1, options, None, 0)
        if status < 0 or result.value is None:
            if result.value is not None:
                self.close(int(result.value))
            raise MemoryFailure('secure_creation_refused')
        return int(result.value)

    def mark_delete(self, handle: int) -> None:
        """Request delete-on-close for an already held object; never resolve a path."""
        disposition = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(wintypes.HANDLE(handle), 4, ctypes.byref(disposition), 1):
            raise MemoryFailure('secure_creation_refused')


_NATIVE_LOCK = threading.Lock()
_NATIVE_INSTANCE: _Native | None = None


def _shared_native() -> _Native:
    global _NATIVE_INSTANCE
    with _NATIVE_LOCK:
        if _NATIVE_INSTANCE is None:
            _NATIVE_INSTANCE = _Native()
        return _NATIVE_INSTANCE


def _descriptor(native: _Native) -> tuple[int, str]:
    system = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    if not native.kernel.GetSystemDirectoryW(system, len(system)):
        raise MemoryFailure('runtime_permissions_unavailable')
    try:
        value = subprocess.run([str(Path(system.value) / 'whoami.exe'), '/user', '/fo', 'csv', '/nh'],
                               capture_output=True, text=True, check=True, timeout=10)
        match = re.search(r'S-1-\d+(?:-\d+)+', value.stdout)
        if match is None:
            raise MemoryFailure('runtime_permissions_unavailable')
        output = wintypes.LPVOID()
        if not native.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                'D:P(A;OICI;FA;;;' + match.group() + ')', 1, ctypes.byref(output), None) or output.value is None:
            raise MemoryFailure('runtime_permissions_unavailable')
        try:
            return int(output.value), native.sddl(int(output.value))
        except BaseException:
            native.kernel.LocalFree(output)
            raise
    except (OSError, subprocess.SubprocessError):
        raise MemoryFailure('runtime_permissions_unavailable') from None


class HeldFile:
    """File created or opened relative to a held directory, never by redirectable path."""
    def __init__(self, tree: 'PrivateTree', handle: int, path: Path, write_share: bool):
        self._tree, self._handle, self.path = tree, handle, path
        self._write_share = write_share
        self._stream: BinaryIO | None = None
        info = tree._native.identity(handle)
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise MemoryFailure('secure_creation_refused')
        tree._native.verify_acl(handle, tree._expected_acl)
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        try:
            self._stream = os.fdopen(fd, 'w+b')
        except BaseException:
            os.close(fd)
            raise
        tree._files.add(self)

    @property
    def stream(self) -> BinaryIO:
        if self._stream is None or self._stream.closed:
            raise MemoryFailure('secure_creation_closed')
        return self._stream

    def identity(self) -> os.stat_result:
        if self._stream is None:
            raise MemoryFailure('secure_creation_closed')
        return os.fstat(self._stream.fileno())

    def verify(self) -> None:
        """Re-check the still-held object; a mutated identity or ACL fails closed."""
        info = self._tree._native.identity(self._handle)
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise MemoryFailure('secure_creation_refused')
        self._tree._native.verify_acl(self._handle, self._tree._expected_acl)

    def release(self) -> None:
        """Close the fd and handle; the file stays on disk with its birth ACL."""
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        self._tree._files.discard(self)
        self._handle = 0

    def close(self) -> None:
        self.release()


class PrivateDirectory:
    """Directory identity held by its enclosing PrivateTree context."""
    def __init__(self, tree: PrivateTree, handle: int, path: Path):
        self._tree, self._handle, self.path = tree, handle, path

    def identity(self) -> os.stat_result:
        self._tree._ensure_open()
        return self._tree._native.identity(self._handle)

    def verify(self) -> None:
        """Re-verify every object this tree created; see PrivateTree.verify_all."""
        self._tree.verify_all()

    def mkdir(self, name: str) -> PrivateDirectory:
        """Create a child directory born with the caller-only descriptor.

        Holds deny directory data/delete opens and replacement. Attribute-only
        junction conversion can still occur while empty; relative creation
        refuses reparse traversal, and verify_all detects a remaining change.
        """
        self._tree._ensure_open()
        handle = self._tree._native.relative(self._handle, name, create=True, directory=True,
                                             descriptor=self._tree._security)
        child = PrivateDirectory(self._tree, handle, self.path / name)
        self._tree._created.append(child)
        try:
            info = child.identity()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise MemoryFailure('secure_creation_refused')
            self._tree._native.verify_acl(handle, self._tree._expected_acl)
        except BaseException:
            self._tree._created.remove(child)
            try:
                self._tree._native.mark_delete(handle)
            finally:
                self._tree._native.close(handle)
            raise
        return child

    def hold_dir(self, name: str) -> PrivateDirectory:
        """Re-open an existing child directory relative to the held handle.

        Used to re-attach to objects a previous attempt of the same flow
        created; identity and the expected private ACL are verified, and the
        reopened hold denies write/delete opens to others.
        """
        self._tree._ensure_open()
        handle = self._tree._native.relative(self._handle, name, create=False, directory=True, share=0x1)
        child = PrivateDirectory(self._tree, handle, self.path / name)
        try:
            info = child.identity()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise MemoryFailure('secure_creation_refused')
            self._tree._native.verify_acl(handle, self._tree._expected_acl)
        except BaseException:
            self._tree._native.close(handle)
            raise
        self._tree._created.append(child)
        return child

    def create_file(self, name: str, *, write_share: bool = False) -> HeldFile:
        """Create a new file born with the caller-only descriptor and hold it.

        File holds never carry DELETE access (Python and SQLite reopen files
        by path without FILE_SHARE_DELETE), so removal always goes through the
        authenticated path-based workspace cleanup. write_share=True keeps the
        file writable by later same-account readers (SQLite) while still
        denying delete/replacement through any other open.
        """
        self._tree._ensure_open()
        handle = self._tree._native.relative(self._handle, name, create=True, directory=False,
                                             descriptor=self._tree._security,
                                             share=0x3 if write_share else 0x1)
        try:
            return HeldFile(self._tree, handle, self.path / name, write_share)
        except BaseException:
            self._tree._native.close(handle)
            raise

    def open_file(self, name: str, *, write_share: bool = False) -> HeldFile:
        """Open an existing child file relative to the held handle."""
        self._tree._ensure_open()
        handle = self._tree._native.relative(self._handle, name, create=False, directory=False,
                                             share=0x3 if write_share else 0x1)
        try:
            return HeldFile(self._tree, handle, self.path / name, write_share)
        except BaseException:
            self._tree._native.close(handle)
            raise

    @contextmanager
    def file(self, name: str) -> Iterator[BinaryIO]:
        held = self.create_file(name)
        try:
            yield held.stream
        finally:
            held.release()


class PrivateTree:
    def __init__(self, parent: Path):
        if sys.platform != 'win32':
            raise MemoryFailure('secure_creation_unsupported')
        if not isinstance(parent, Path) or not parent.is_absolute() or '..' in parent.parts:
            raise MemoryFailure('absolute_runtime_path_required')
        # The walk input stays raw: resolving here would follow a reparse
        # point at the final component and silently redirect every operation.
        raw = parent.lstat()
        if not stat.S_ISDIR(raw.st_mode) or getattr(raw, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise MemoryFailure('secure_creation_refused')
        try:
            # Canonical long path: derived published paths and registration
            # must match what later path-based flows resolve to.
            self._canonical = external(parent)
            self._expected = self._canonical.stat()
        except OSError:
            raise MemoryFailure('secure_creation_refused') from None
        self._parent = parent
        self._native = _shared_native()
        self._handles: list[int] = []
        self._created: list[PrivateDirectory] = []
        self._files: set[HeldFile] = set()
        self._security: int | None = None
        self._expected_acl = ''
        self._closed = False
        self._owner: int | None = None

    def _ensure_open(self) -> None:
        if self._closed or self._security is None:
            raise MemoryFailure('secure_creation_closed')
        if self._owner != threading.get_ident():
            raise MemoryFailure('secure_creation_wrong_thread')

    def delete_created(self) -> None:
        """Disposition-delete created directories, children first, then close.

        Uses only held handles, so replacement or redirection cannot move the
        deletion target; ancestors opened during the anchor walk are untouched.
        File holds never carry DELETE access, so they are only released and
        left for the authenticated path-based workspace cleanup; a directory
        that still contains such files refuses deletion and fails loudly.
        """
        self._ensure_open()
        failure: BaseException | None = None
        for held in tuple(self._files):
            try:
                held.release()
            except BaseException as error:
                if failure is None:
                    failure = error
        for directory in reversed(self._created):
            try:
                self._native.mark_delete(directory._handle)
                self._native.close(directory._handle)
                directory._handle = 0
            except BaseException as error:
                if failure is None:
                    failure = error
        self._created.clear()
        if failure is not None:
            raise failure

    def verify_all(self) -> None:
        """Re-verify every created object through its held handle.

        Detects in-place mutation of a held directory (for example a junction
        conversion through a write-attributes open) or any ACL drift, so a
        redirection attempt becomes a detected abort instead of silent damage.
        """
        self._ensure_open()
        for directory in self._created:
            info = self._native.identity(directory._handle)
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise MemoryFailure('secure_creation_refused')
            self._native.verify_acl(directory._handle, self._expected_acl)
        for held in tuple(self._files):
            held.verify()

    def __enter__(self) -> PrivateDirectory:
        if self._closed or self._handles:
            raise MemoryFailure('secure_creation_closed')
        self._owner = threading.get_ident()
        try:
            anchor = self._parent.anchor
            if self._native.kernel.GetDriveTypeW(anchor) != 3:  # DRIVE_FIXED
                raise MemoryFailure('secure_creation_unsupported')
            # The walk may re-enter directories this process holds with DELETE
            # access (nested managed trees), so it shares every access class.
            handle = self._native.kernel.CreateFileW(anchor, 0x001200a1, 0x7, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise MemoryFailure('secure_creation_refused')
            self._handles.append(int(handle))
            for part in self._parent.parts[1:]:
                handle = self._native.relative(int(handle), part, create=False, directory=True, share=0x7)
                self._handles.append(int(handle))
                info = self._native.identity(int(handle))
                if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise MemoryFailure('secure_creation_refused')
            final = self._native.identity(int(handle))
            if (final.st_dev, final.st_ino) != (self._expected.st_dev, self._expected.st_ino):
                raise MemoryFailure('secure_creation_refused')
            filesystem = ctypes.create_unicode_buffer(32)
            if (not self._native.kernel.GetVolumeInformationByHandleW(handle, None, 0, None, None, None,
                                                                       filesystem, len(filesystem))
                    or filesystem.value.upper() != 'NTFS'):
                raise MemoryFailure('secure_creation_unsupported')
            self._security, self._expected_acl = _descriptor(self._native)
            return PrivateDirectory(self, int(handle), self._canonical)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._owner is not None and self._owner != threading.get_ident():
            raise MemoryFailure('secure_creation_wrong_thread')
        if not self._closed:
            failure: BaseException | None = None
            try:
                for held in tuple(self._files):
                    try:
                        held.release()
                    except BaseException as error:
                        if failure is None:
                            failure = error
            finally:
                while self._created:
                    directory = self._created.pop()
                    if directory._handle:
                        self._native.close(directory._handle)
                        directory._handle = 0
                while self._handles:
                    self._native.close(self._handles.pop())
                if self._security is not None:
                    self._native.kernel.LocalFree(wintypes.HLOCAL(self._security))
                    self._security = None
                self._closed = True
            if failure is not None:
                raise failure

    def __exit__(self, *args: object) -> None:
        self.close()
