"""Read NTFS DACLs for the runtime's current-user-only policy."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import cache
from pathlib import Path
import struct

from ..models import MemoryFailure


_FILE_ALL_ACCESS = 0x1F01FF
_SE_DACL_PROTECTED = 0x1000
_INHERIT_ONLY_ACE = 0x08


@cache
def _apis() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = wintypes.LPVOID
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ptr, wintypes.DWORD, wintypes.LPDWORD)
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetLengthSid.argtypes = (ptr,)
    advapi.GetLengthSid.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ptr, ptr,
                                            ctypes.POINTER(ptr), ptr, ctypes.POINTER(ptr))
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = (ptr, ctypes.POINTER(wintypes.WORD), wintypes.LPDWORD)
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.IsValidAcl.argtypes = (ptr,)
    advapi.IsValidAcl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.LocalFree.restype = wintypes.HLOCAL
    return advapi, kernel


def current_user_sid() -> bytes:
    advapi, _ = _apis()
    # GetCurrentThreadEffectiveToken() is the Win32 inline pseudo-handle -6.
    # TOKEN_USER (SID_AND_ATTRIBUTES) and the largest Windows SID fit in 128 bytes.
    token_user = ctypes.create_string_buffer(128)
    size = wintypes.DWORD()
    if not advapi.GetTokenInformation(-6, 1, token_user, len(token_user), ctypes.byref(size)):
        raise MemoryFailure("runtime_permissions_unavailable")
    sid = ctypes.c_void_p.from_buffer(token_user).value
    if sid is None:
        raise MemoryFailure("runtime_permissions_unavailable")
    return ctypes.string_at(sid, advapi.GetLengthSid(sid))


def _current_user_only(acl: bytes, sid: bytes) -> bool:
    # ACL and ACE sizes/counts were validated by IsValidAcl before copying.
    count = struct.unpack_from("<H", acl, 4)[0]
    offset = 8  # sizeof(ACL)
    full = False
    for _ in range(count):
        kind, flags, size = struct.unpack_from("<BBH", acl, offset)
        if kind != 0 or size != 8 + len(sid):  # Plain ACCESS_ALLOWED_ACE only.
            return False
        rights = struct.unpack_from("<I", acl, offset + 4)[0]
        if acl[offset + 8:offset + size] != sid:
            return False
        if not flags & _INHERIT_ONLY_ACE and rights & _FILE_ALL_ACCESS == _FILE_ALL_ACCESS:
            full = True
        offset += size
    return full


def verify_dacl(path: Path, sid: bytes, *, protected: bool) -> None:
    advapi, kernel = _apis()
    descriptor = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    # GetNamedSecurityInfo allocates the snapshot and returns the DACL inside it.
    error = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(dacl),
                                        None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        if not descriptor.value or not dacl.value or not advapi.IsValidAcl(dacl):
            raise MemoryFailure("unsafe_runtime_permissions")
        if protected:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if (not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                    or not control.value & _SE_DACL_PROTECTED):
                raise MemoryFailure("unsafe_runtime_permissions")
        acl_size = wintypes.WORD.from_address(dacl.value + 2).value
        if not _current_user_only(ctypes.string_at(dacl, acl_size), sid):
            raise MemoryFailure("unsafe_runtime_permissions")
    finally:
        kernel.LocalFree(descriptor)
