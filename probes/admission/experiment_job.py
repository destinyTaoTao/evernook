"""A capability-owned Windows experiment, including nested worker jobs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from types import TracebackType
from typing import Any, BinaryIO, Self


def _kernel() -> Any:
    if sys.platform != 'win32':
        raise RuntimeError('experiment_requires_windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    return kernel


class ExperimentJob:
    def __init__(self) -> None:
        from evernook.worker import _ExtendedLimits
        self.kernel = _kernel()
        self.handle: int | None = self.kernel.CreateJobObjectW(None, None)
        self.process: subprocess.Popen[bytes] | None = None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise RuntimeError('experiment_job_unavailable')

    def _open(self) -> int:
        if self.handle is None:
            raise RuntimeError('experiment_job_closed')
        return self.handle

    def start(self, command: list[str], stdout: BinaryIO, stderr: BinaryIO) -> subprocess.Popen[bytes]:
        if self.process is not None:
            raise RuntimeError('experiment_already_started')
        handle = self._open()
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [handle]}
        # Only this capability reaches the small stdlib-only wrapper. It joins
        # before starting the target and closes its copy before target creation.
        os.set_handle_inheritable(handle, True)
        try:
            base = str(getattr(sys, '_base_executable', None) or sys.executable)
            self.process = subprocess.Popen([base, '-B', str(Path(__file__).resolve()), '--worker', str(handle), *command],
                stdout=stdout, stderr=stderr, startupinfo=startup, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            os.set_handle_inheritable(handle, False)
        return self.process

    def pids(self) -> list[int]:
        capacity = 16
        while capacity <= 65536:
            buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
            success = self.kernel.QueryInformationJobObject(self._open(), 3, buffer, len(buffer), None)
            total, returned = (wintypes.DWORD * 2).from_buffer(buffer)
            if success and returned <= capacity and total <= returned:
                return [int(pid) for pid in (ctypes.c_size_t * returned).from_buffer(buffer, 8)]
            if not success and ctypes.get_last_error() != 234:
                raise RuntimeError('experiment_members_unavailable')
            capacity = max(capacity * 2, int(total) + 16)
        raise RuntimeError('experiment_members_overflow')

    def contains(self, process_handle: int) -> bool:
        member = wintypes.BOOL()
        if not self.kernel.IsProcessInJob(process_handle, self._open(), ctypes.byref(member)):
            raise RuntimeError('experiment_membership_unavailable')
        return bool(member.value)

    def created(self, process_handle: int) -> str:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(process_handle, *(ctypes.byref(value) for value in times)):
            raise RuntimeError('experiment_process_time_unavailable')
        return str((int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime))

    def terminate(self, child: subprocess.Popen[bytes]) -> None:
        if not self.kernel.TerminateJobObject(self._open(), 1):
            raise RuntimeError('experiment_stop_failed')
        if child.poll() is None:
            # Covers the tiny wrapper-bootstrap interval before it joins.
            child.kill()
        child.wait(timeout=10)

    def close(self) -> None:
        if self.handle is None:
            return
        try:
            if self.process is not None and self.process.poll() is None:
                self.terminate(self.process)
        finally:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, trace: TracebackType | None) -> None:
        self.close()


def _worker() -> int:
    # No Core/optional package imports before joining the parent's job.
    if len(sys.argv) < 4 or sys.argv[1] != '--worker':
        return 2
    kernel = _kernel()
    handle = int(sys.argv[2])
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise RuntimeError('experiment_assignment_failed')
    finally:
        kernel.CloseHandle(handle)
    return subprocess.run(sys.argv[3:], creationflags=subprocess.CREATE_NO_WINDOW).returncode


if __name__ == '__main__':
    raise SystemExit(_worker())
