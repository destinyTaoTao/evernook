"""Cross-process shared connections and an exclusive service cutover window."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import msvcrt
from pathlib import Path
import time

from .models import MemoryFailure
from .paths import external


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


@contextmanager
def ownership_gate(root: Path, *, exclusive: bool, timeout: float,
                   check: Callable[[], float] | None = None) -> Iterator[None]:
    target = external(root / "memory-service-gate.lock")
    if target.parent != root:
        raise MemoryFailure("unsafe_runtime_path")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped))
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped))
    kernel.UnlockFileEx.restype = wintypes.BOOL
    with target.open("a+b") as stream:
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlapped = _Overlapped()
        deadline = time.monotonic() + timeout
        while not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
            if ctypes.get_last_error() != 33:
                raise MemoryFailure("service_gate_unavailable")
            if check is not None:
                check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryFailure("storage_busy")
            time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
