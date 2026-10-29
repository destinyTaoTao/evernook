"""Fixed local Windows/NTFS scope and owner-only runtime directory provisioning."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import re
import subprocess
import sys

from .models import MemoryFailure


def external(path: Path) -> Path:
    if not path.is_absolute():
        raise MemoryFailure("absolute_runtime_path_required")
    resolved = path.resolve()
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


def verify_private_directory(root: Path) -> None:
    """Check effective file grants before loading secrets; never silently repair existing ACLs."""
    directory = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory)):
        raise MemoryFailure("runtime_permissions_unavailable")
    executable = Path(directory.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = """
        $ErrorActionPreference = 'Stop'
        try {
            [Console]::InputEncoding = [Text.UTF8Encoding]::new()
            $paths = [Console]::In.ReadToEnd() | ConvertFrom-Json
            $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            foreach ($path in $paths) {
                if ([IO.Directory]::Exists($path)) {
                    $acl = [IO.Directory]::GetAccessControl($path)
                } elseif ([IO.File]::Exists($path)) {
                    $acl = [IO.File]::GetAccessControl($path)
                } else { continue }
                if ($path -eq $paths[0] -and !$acl.AreAccessRulesProtected) { exit 1 }
                $full = $false
                foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                    if ($rule.AccessControlType -eq 'Deny') { exit 1 }
                    if ($rule.IdentityReference.Value -ne $sid) { exit 1 }
                    $rights = [Security.AccessControl.FileSystemRights]::FullControl
                    if (($rule.FileSystemRights -band $rights) -eq $rights) { $full = $true }
                }
                if (!$full) { exit 1 }
            }
            [Console]::Write('ok')
        } catch { exit 1 }
    """
    paths = [root] + [root / name for name in ("runtime.json", "client.json", "idempotency.key", "memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm")]
    try:
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                                input=json.dumps([str(path) for path in paths]), encoding="utf-8",
                                capture_output=True, check=True, timeout=15)
        if result.stdout != "ok":
            raise MemoryFailure("unsafe_runtime_permissions")
    except (OSError, subprocess.SubprocessError):
        raise MemoryFailure("unsafe_runtime_permissions") from None
