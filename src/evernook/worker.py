"""Windows host-owned CLI lifetime: cancellation reaps the full descendant job."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading

from .models import MemoryFailure


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_job: int | None = None


def protect_parent_lifetime() -> None:
    """One startup guard. Monitor real handles; never terminate a parent or a reused PID."""
    global _job
    if _job is not None:
        return
    if sys.platform != "win32":
        raise MemoryFailure("unsupported_platform")
    try:
        owner = int(os.environ.get("EVERNOOK_WORKER_PARENT_PID", str(os.getppid())))
        if not 0 < owner < 2 ** 32 or owner == os.getpid():
            raise ValueError
    except ValueError:
        raise MemoryFailure("worker_lifetime_unavailable") from None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForMultipleObjects.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD)
    kernel.WaitForMultipleObjects.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    pipe_handle: int | None = None
    if os.environ.get('EVERNOOK_WORKER_LIFETIME_PIPE') == '1':
        try:
            import msvcrt
            pipe_handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            kernel.GetFileType.argtypes = (wintypes.HANDLE,)
            kernel.GetFileType.restype = wintypes.DWORD
            kernel.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
            kernel.PeekNamedPipe.restype = wintypes.BOOL
            available = wintypes.DWORD()
            if (kernel.GetFileType(pipe_handle) != 3
                    or not kernel.PeekNamedPipe(pipe_handle, None, 0, None, ctypes.byref(available), None)
                    or available.value):
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise MemoryFailure('worker_lifetime_unavailable') from None
    parents: list[int] = []
    job: int | None = None
    try:
        for process_id in {os.getppid(), owner}:
            handle: int = kernel.OpenProcess(0x100000, False, process_id)  # SYNCHRONIZE only.
            if not handle:
                raise MemoryFailure("worker_lifetime_unavailable")
            parents.append(handle)
        job = kernel.CreateJobObjectW(None, None)
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
            raise MemoryFailure("worker_lifetime_unavailable")
    except MemoryFailure:
        if job:
            kernel.CloseHandle(job)
        for handle in parents:
            kernel.CloseHandle(handle)
        raise
    _job = job
    # Keep the non-inheritable job handle open until process teardown. Closing it here
    # would terminate this worker before its normal CLI reply/exit can complete.
    handles = (wintypes.HANDLE * len(parents))(*parents)
    def owner_exited() -> None:
        kernel.WaitForMultipleObjects(len(parents), handles, False, 0xFFFFFFFF)
        kernel.TerminateJobObject(job, 1)
    threading.Thread(target=owner_exited, name="evernook-worker-owner", daemon=True).start()
    if pipe_handle is not None:
        def pipe_closed() -> None:
            # A pending synchronous pipe read can block another process that
            # inherits the same pipe. Poll without consuming input instead.
            wake = threading.Event()
            try:
                while not wake.wait(0.05):
                    available = wintypes.DWORD()
                    if (not kernel.PeekNamedPipe(pipe_handle, None, 0, None, ctypes.byref(available), None)
                            or available.value):
                        break
            except Exception:
                pass
            if not kernel.TerminateJobObject(job, 1):
                os._exit(1)  # Closing this process's job handle also terminates its job.
        threading.Thread(target=pipe_closed, name='evernook-worker-pipe', daemon=True).start()


def main() -> None:
    try:
        protect_parent_lifetime()
    except (MemoryFailure, OSError, RuntimeError) as error:
        sys.stdout.buffer.write(json.dumps({"contract_version": 1, "ok": False,
            "error": {"code": error.code if isinstance(error, MemoryFailure) else "worker_lifetime_unavailable"}}).encode() + b"\n")
        sys.stdout.buffer.flush()
        raise SystemExit(1)
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
