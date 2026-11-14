"""Fail-closed read-only check of the current Windows session lock state."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import sys
from typing import Any


class _LevelOnePrefix(ctypes.Structure):
    _fields_ = [('session_id', wintypes.DWORD), ('state', ctypes.c_int32), ('flags', ctypes.c_int32)]


class _InfoUnion(ctypes.Union):
    # The documented union contains LARGE_INTEGER fields and is eight-byte aligned.
    _fields_ = [('level_one', _LevelOnePrefix), ('alignment', ctypes.c_int64)]


class _InfoPrefix(ctypes.Structure):
    _fields_ = [('level', wintypes.DWORD), ('data', _InfoUnion)]


def _valid_info(raw: bytes, session_id: int) -> bool:
    if len(raw) < ctypes.sizeof(_InfoPrefix):
        return False
    info = _InfoPrefix.from_buffer_copy(raw)
    return bool(info.level == 1 and info.data.level_one.session_id == session_id
                and info.data.level_one.state == 0 and info.data.level_one.flags == 1)


@lru_cache(maxsize=1)
def _kernel() -> Any:
    return ctypes.WinDLL('kernel32', use_last_error=True)


@lru_cache(maxsize=1)
def _wts() -> Any:
    return ctypes.WinDLL('wtsapi32', use_last_error=True)


@lru_cache(maxsize=1)
def _apis() -> tuple[Any, Any]:
    # Cache each load separately so a later failure cannot repeatedly load kernel32.
    kernel, wts = _kernel(), _wts()
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
    wts.WTSFreeMemory.restype = None
    return kernel, wts


def current_session_unlocked() -> bool:
    """WTSActive + WTS_SESSIONSTATE_UNLOCK; never assume success on unavailable state."""
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
        return False
    try:
        # Windows 7/2008 R2 report the documented lock flags in reverse.
        if sys.getwindowsversion()[:2] < (6, 2):
            return False
        kernel, wts = _apis()
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
            return False
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD()
        # WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionInfoEx.
        queried = wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 25, ctypes.byref(buffer), ctypes.byref(size))
        try:
            if not queried or not buffer.value or not ctypes.sizeof(_InfoPrefix) <= size.value <= 65536:
                return False
            return _valid_info(ctypes.string_at(buffer, size.value), session.value)
        finally:
            if buffer.value:
                wts.WTSFreeMemory(buffer)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
