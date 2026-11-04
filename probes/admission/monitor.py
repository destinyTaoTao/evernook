"""Run an owned experiment and sample its live Windows process tree every 250 ms."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import math
from pathlib import Path
import subprocess
import time

from evernook.paths import external
from scale_ingest import MemoryCounters, save


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
               ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
               ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
               ("exe", wintypes.WCHAR * 260)]


def stop_owned_tree(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    directory = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory)):
        raise RuntimeError("system_directory_unavailable")
    subprocess.run([str(Path(directory.value) / "taskkill.exe"), "/PID", str(child.pid), "/T", "/F"],
        capture_output=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    child.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=900,
                        help="Live-root budget; attempts owned-tree termination (default: 900 seconds)")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("command required")
    if not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        parser.error("max-seconds must be finite and positive")
    root = external(args.output)
    root.mkdir()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    query.restype = wintypes.BOOL
    started = time.perf_counter()
    peak = 0
    samples = 0
    incomplete = 0
    stop_reason = None
    stop_error = None
    with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        save(root / "manifest.json", {"command": command, "pid": child.pid, "sample_interval_ms": 250,
             "max_seconds": args.max_seconds, "stop_file": str(root / "STOP"),
             "scope": "live owned process tree; short-lived children between samples and post-root orphans may be missed; no hard peak guarantee"})
        with (root / "resources.jsonl").open("w", encoding="utf-8") as log:
            while child.poll() is None:
                tick = time.perf_counter()
                if (root / "STOP").exists() or tick - started >= args.max_seconds:
                    stop_reason = "operator_stop" if (root / "STOP").exists() else "execution_time_budget"
                    try:
                        stop_owned_tree(child)
                    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                        stop_error = type(error).__name__
                    break
                parents: dict[int, int] = {}
                snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
                if snapshot == ctypes.c_void_p(-1).value:
                    incomplete += 1
                else:
                    try:
                        row = ProcessEntry()
                        row.size = ctypes.sizeof(row)
                        more = kernel.Process32FirstW(snapshot, ctypes.byref(row))
                        while more:
                            parents[int(row.pid)] = int(row.parent)
                            more = kernel.Process32NextW(snapshot, ctypes.byref(row))
                    finally:
                        kernel.CloseHandle(snapshot)
                owned = {child.pid}
                while True:
                    found = {pid for pid, parent in parents.items() if parent in owned}
                    if found <= owned:
                        break
                    owned |= found
                processes = []
                for pid in sorted(owned):
                    handle = kernel.OpenProcess(0x410, False, pid)
                    if not handle:
                        incomplete += 1
                        continue
                    try:
                        memory = MemoryCounters()
                        memory.cb = ctypes.sizeof(memory)
                        if query(handle, ctypes.byref(memory), memory.cb):
                            processes.append({"pid": pid, "private_bytes": int(memory.private), "working_set_bytes": int(memory.working)})
                        else:
                            incomplete += 1
                    finally:
                        kernel.CloseHandle(handle)
                total = sum(row["private_bytes"] for row in processes)
                peak = max(peak, total)
                samples += 1
                log.write(json.dumps({"elapsed_ms": (tick - started) * 1000,
                    "observed_at_unix_ms": time.time() * 1000, "private_bytes": total, "processes": processes}) + "\n")
                log.flush()
                time.sleep(max(0, .25 - (time.perf_counter() - tick)))
        code = child.poll() if stop_reason else child.wait()
    save(root / "report.json", {"returncode": code, "elapsed_seconds": time.perf_counter() - started,
         "stop_reason": stop_reason, "stop_error": stop_error, "root_exited": code is not None,
         "max_seconds": args.max_seconds,
         "samples": samples, "query_failures": incomplete, "peak_observed_tree_private_bytes": peak})
    if stop_reason:
        return 2
    assert code is not None
    return code


if __name__ == "__main__":
    raise SystemExit(main())
