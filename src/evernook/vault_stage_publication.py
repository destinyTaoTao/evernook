"""Handle-fenced atomic publication of a single staged bundle on Windows."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import msvcrt
import os
from pathlib import Path
import stat
from typing import BinaryIO, Callable, Iterator

from .models import MemoryFailure


@contextmanager
def publish_guard(path: Path, identity: os.stat_result, destination_parent: Path,
                  parent_identity: os.stat_result, check: Callable[[], None]
                  ) -> Iterator[tuple[BinaryIO, Callable[[Path], None]]]:
    """Deny writes and replacement; publish the same verified open file handle."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    check()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise MemoryFailure('vault_workspace_unsafe')
    parent_before = destination_parent.lstat()
    if (not stat.S_ISDIR(parent_before.st_mode)
            or getattr(parent_before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (parent_before.st_dev, parent_before.st_ino) != (parent_identity.st_dev, parent_identity.st_ino)):
        raise MemoryFailure('vault_workspace_unsafe')
    parent_handle = kernel.CreateFileW(str(destination_parent), 0x80 | 0x20, 0x1 | 0x2, None, 3,
                                       0x02000000 | 0x00200000, None)
    if parent_handle == wintypes.HANDLE(-1).value:
        raise MemoryFailure('vault_workspace_busy')
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x10000 | 0x80, 0x1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        kernel.CloseHandle(parent_handle)
        raise MemoryFailure('vault_workspace_busy')
    try:
        duplicate = wintypes.HANDLE()
        current = kernel.GetCurrentProcess()
        parent_duplicate = wintypes.HANDLE()
        if not kernel.DuplicateHandle(current, parent_handle, current, ctypes.byref(parent_duplicate), 0, False, 2) or parent_duplicate.value is None:
            raise MemoryFailure('vault_workspace_busy')
        try:
            parent_fd = msvcrt.open_osfhandle(parent_duplicate.value, os.O_RDONLY)
        except BaseException:
            kernel.CloseHandle(parent_duplicate)
            raise
        try:
            actual_parent = os.fstat(parent_fd)
        finally:
            os.close(parent_fd)
        if ((actual_parent.st_dev, actual_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino)
                or getattr(actual_parent, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise MemoryFailure('vault_workspace_unsafe')
        if not kernel.DuplicateHandle(current, handle, current, ctypes.byref(duplicate), 0, False, 2) or duplicate.value is None:
            raise MemoryFailure('vault_workspace_busy')
        try:
            fd = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(duplicate)
            raise
        with os.fdopen(fd, 'rb') as stream:
            actual = os.fstat(stream.fileno())
            if (actual.st_dev != identity.st_dev or actual.st_ino != identity.st_ino
                    or before.st_dev != actual.st_dev or before.st_ino != actual.st_ino
                    or getattr(actual, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise MemoryFailure('vault_workspace_unsafe')
            def rename(destination: Path) -> None:
                check()
                if destination.parent != destination_parent:
                    raise MemoryFailure('unsafe_backup_path')
                name = str(destination)
                name_bytes = len(name.encode('utf-16-le'))
                class RenameInfo(ctypes.Structure):
                    _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                                ('length', wintypes.DWORD), ('name', ctypes.c_wchar * (name_bytes // 2 + 1))]
                info = RenameInfo(0, None, name_bytes, name)
                if not kernel.SetFileInformationByHandle(handle, 3, ctypes.byref(info), RenameInfo.name.offset + info.length):
                    raise MemoryFailure('vault_stage_publication_failed')
            yield stream, rename
    finally:
        kernel.CloseHandle(handle)
        kernel.CloseHandle(parent_handle)
