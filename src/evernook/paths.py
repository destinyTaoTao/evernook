"""Fixed local Windows/NTFS scope and owner-only runtime directory provisioning."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import stat
import subprocess
import sys

from .models import MemoryFailure
from .windows_acl import current_user_sid, verify_dacl


def external(path: Path) -> Path:
    if not path.is_absolute():
        raise MemoryFailure("absolute_runtime_path_required")
    resolved = path.resolve()
    # Windows may retain this prefix when a SQLite journal vanishes during
    # resolve. Compare the same local DOS namespace; UNC/device paths stay distinct.
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", str(resolved)):
        resolved = Path(str(resolved)[4:])
    if resolved.is_relative_to(Path(__file__).resolve().parents[2]):
        raise MemoryFailure("runtime_inside_checkout")
    if sys.platform != "win32":
        raise MemoryFailure("unsupported_platform")
    kernel = ctypes.windll.kernel32
    if int(kernel.GetDriveTypeW(resolved.anchor)) != 3:
        raise MemoryFailure("local_fixed_ntfs_required")
    filesystem = ctypes.create_unicode_buffer(256)
    if not kernel.GetVolumeInformationW(resolved.anchor, None, 0, None, None, None, filesystem, len(filesystem)) or filesystem.value != "NTFS":
        raise MemoryFailure("local_fixed_ntfs_required")
    return resolved


def provision_private_directory(root: Path) -> None:
    root = external(root)
    root.mkdir()  # Refuse to overwrite or change permissions on an existing runtime directory.
    directory = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory)):
        raise MemoryFailure("runtime_permissions_unavailable")
    system = Path(directory.value)
    try:
        identity = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True, timeout=10)
        match = re.search(r"S-1-\d+(?:-\d+)+", identity.stdout)
        if not match:
            raise MemoryFailure("runtime_permissions_unavailable")
        subprocess.run([str(system / "icacls.exe"), str(root), "/inheritance:r", "/grant:r", f"*{match.group()}:(OI)(CI)F"],
                       capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise MemoryFailure("runtime_permissions_unavailable") from None


def verify_private_directory(root: Path, *, extra_files: tuple[Path, ...] = ()) -> None:
    """Inspect fresh DACLs before loading secrets; never repair existing grants."""
    journals = (root / "memory.sqlite3-wal", root / "memory.sqlite3-shm")
    paths = [root, *(root / name for name in ("runtime.json", "client.json", "idempotency.key", "memory.sqlite3")),
             *journals]
    for path in extra_files:
        resolved = external(path)
        if resolved.parent != root:
            raise MemoryFailure("unsafe_runtime_path")
        if resolved not in paths:
            paths.append(resolved)

    try:
        sid = current_user_sid()
        for attempt in range(3):
            for path in paths:
                try:
                    info = path.lstat()
                except FileNotFoundError as error:
                    # A private backup/workspace may lack even the usual runtime files.
                    # Only an absent leaf is optional; its parent must still exist.
                    if path != root and error.winerror == 2:
                        continue
                    raise
                if (info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or (path == root and not stat.S_ISDIR(info.st_mode))):
                    raise MemoryFailure("unsafe_runtime_permissions")
                try:
                    verify_dacl(path, sid, protected=path == root)
                except FileNotFoundError as error:
                    # SQLite removed a journal after lstat. Recheck the whole set.
                    if path in journals and error.winerror == 2 and attempt < 2:
                        break
                    raise
            else:
                return
    except (OSError, AttributeError):
        raise MemoryFailure("unsafe_runtime_permissions") from None
