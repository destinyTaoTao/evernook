"""Sample verified Windows Job members; PID parent links are not ownership."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import hashlib
import math
from pathlib import Path
import subprocess
import time
from typing import TypedDict

from evernook.paths import external
from probes.admission.scale_ingest import MemoryCounters, save
from probes.admission.experiment_job import ExperimentJob


class ProcessSample(TypedDict):
    pid: int
    private_bytes: int
    working_set_bytes: int
    created_filetime: str
    ownership: str


@contextmanager
def owned_job(cleanup_errors: list[str]) -> Iterator[ExperimentJob]:
    job = ExperimentJob()
    try:
        yield job
    finally:
        try:
            job.close()
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            # close still releases the KILL_ON_JOB_CLOSE handle. Retain the
            # failure and check the actual wrapper exit before reporting it.
            cleanup_errors.append(type(error).__name__)


def sample_job(job: ExperimentJob, child: subprocess.Popen[bytes]) -> tuple[list[ProcessSample], int]:
    kernel = job.kernel
    query = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD)
    query.restype = wintypes.BOOL
    failures = 0
    try:
        pids = set(job.pids()) | {child.pid}
    except RuntimeError:
        pids, failures = {child.pid}, 1
    processes: list[ProcessSample] = []
    for pid in sorted(pids):
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            failures += 1
            continue
        try:
            # A PID returned by enumeration can already have been reused when
            # OpenProcess runs. Verify this exact handle, not a cached PID edge.
            if pid != child.pid and not job.contains(handle):
                failures += 1
                continue
            created = job.created(handle)
            memory = MemoryCounters()
            memory.cb = ctypes.sizeof(memory)
            if query(handle, ctypes.byref(memory), memory.cb):
                processes.append({'pid': pid, 'private_bytes': int(memory.private),
                    'working_set_bytes': int(memory.working), 'created_filetime': created,
                    'ownership': 'launch_handle' if pid == child.pid else 'verified_job_member'})
            else:
                failures += 1
        except RuntimeError:
            failures += 1
        finally:
            kernel.CloseHandle(handle)
    return processes, failures


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
    started = time.perf_counter()
    peak = 0
    samples = 0
    incomplete = 0
    stop_reason = None
    stop_error = None
    cleanup_errors: list[str] = []
    with owned_job(cleanup_errors) as job, (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
        child = job.start(command, stdout, stderr)
        save(root / "manifest.json", {"command": command, "pid": child.pid, "sample_interval_ms": 250,
             "harness_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ('monitor.py', 'experiment_job.py')},
             "max_seconds": args.max_seconds, "stop_file": str(root / "STOP"),
             'ownership': 'windows_job_v1',
             "scope": "Kernel job including nested jobs + exact launched wrapper; membership verified on opened handles; wrapper overhead included, short-lived peaks between samples may be missed; no hard peak guarantee"})
        with (root / "resources.jsonl").open("w", encoding="utf-8") as log:
            while child.poll() is None:
                tick = time.perf_counter()
                if (root / "STOP").exists() or tick - started >= args.max_seconds:
                    stop_reason = "operator_stop" if (root / "STOP").exists() else "execution_time_budget"
                    try:
                        job.terminate(child)
                    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                        stop_error = type(error).__name__
                    break
                processes, failures = sample_job(job, child)
                incomplete += failures
                total = sum(row["private_bytes"] for row in processes)
                peak = max(peak, total)
                samples += 1
                log.write(json.dumps({"elapsed_ms": (tick - started) * 1000,
                    "observed_at_unix_ms": time.time() * 1000, "private_bytes": total, "processes": processes}) + "\n")
                log.flush()
                time.sleep(max(0, .25 - (time.perf_counter() - tick)))
        code = child.poll() if stop_reason else child.wait()
    if code is None:
        try:
            code = child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cleanup_errors.append('TimeoutExpired')
    save(root / "report.json", {"returncode": code, "elapsed_seconds": time.perf_counter() - started,
         "stop_reason": stop_reason, "stop_error": stop_error, "root_exited": code is not None,
         "cleanup_errors": cleanup_errors,
         "max_seconds": args.max_seconds,
         'ownership': 'windows_job_v1',
         "samples": samples, "query_failures": incomplete, "peak_observed_tree_private_bytes": peak})
    if stop_reason or cleanup_errors:
        return 2
    assert code is not None
    return code


if __name__ == "__main__":
    raise SystemExit(main())
