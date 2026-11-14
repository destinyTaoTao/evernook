from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from evernook import initialize, pair_client


class WorkerLifetimeTests(unittest.TestCase):
    def exercise(self, mode: str) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handles: list[int] = []
        launcher = Path(sys.prefix) / "Scripts/python.exe"
        with subprocess.Popen([str(launcher), "-u", str(Path(__file__).with_name("owned_worker_fixture.py")), mode],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW) as parent:
            try:
                assert parent.stdout is not None
                with ThreadPoolExecutor(max_workers=1) as reader:
                    line = reader.submit(parent.stdout.readline).result(timeout=10)
                self.assertTrue(line, "Owned worker did not report process identities")
                identities = json.loads(line)
                for process_id in set(identities):
                    handle: int = kernel.OpenProcess(0x100001, False, process_id)
                    self.assertTrue(handle)
                    handles.append(handle)
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                parent.terminate()
                parent.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0, "Owned descendant survived parent cancellation")
            finally:
                if parent.poll() is None:
                    parent.kill()
                for handle in reversed(handles):
                    kernel.TerminateProcess(handle, 1)
                    kernel.WaitForSingleObject(handle, 5000)
                    kernel.CloseHandle(handle)

    def test_canceling_virtualenv_launcher_terminates_real_worker_and_all_descendants(self) -> None:
        self.exercise("worker")

    def test_owner_exit_terminates_descendants_even_if_an_intermediate_launcher_remains(self) -> None:
        self.exercise("owner")

    def test_closed_owner_pipe_terminates_job_while_owner_is_still_alive(self) -> None:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handles: list[int] = []
        launcher = Path(sys.prefix) / 'Scripts/python.exe'
        with subprocess.Popen([str(launcher), '-u', str(Path(__file__).with_name('owned_worker_fixture.py')), 'pipe-owner'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW) as parent:
            try:
                assert parent.stdout is not None and parent.stdin is not None
                lines: list[bytes] = []
                ready = threading.Event()
                def read_line() -> None:
                    assert parent.stdout is not None
                    lines.append(parent.stdout.readline()); ready.set()
                threading.Thread(target=read_line, daemon=True).start()
                self.assertTrue(ready.wait(10))
                line = lines[0]
                self.assertTrue(line)
                for process_id in set(json.loads(line)):
                    handle: int = kernel.OpenProcess(0x100001, False, process_id)
                    self.assertTrue(handle)
                    handles.append(handle)
                parent.stdin.write(b'x'); parent.stdin.flush()
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                self.assertIsNone(parent.poll())
            finally:
                if parent.poll() is None:
                    parent.kill()
                for handle in handles:
                    kernel.TerminateProcess(handle, 1)
                    kernel.CloseHandle(handle)

    def test_pipe_required_with_no_pipe_fails_before_private_window(self) -> None:
        for operation in ('service-private-window', 'service-private-session'):
            result = subprocess.run([str(Path(sys.prefix) / 'Scripts/python.exe'), '-m', 'evernook.private_window_worker', operation],
                env={**os.environ, 'EVERNOOK_WORKER_PARENT_PID': str(os.getpid()), 'EVERNOOK_WORKER_LIFETIME_PIPE': '1'},
                stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'worker_lifetime_unavailable')

    def test_private_window_entry_rejects_missing_pipe_flag_and_other_commands(self) -> None:
        for operation, flag in (('service-private-window', ''), ('status', '1')):
            result = subprocess.run([str(Path(sys.prefix) / 'Scripts/python.exe'), '-m', 'evernook.private_window_worker', operation],
                env={**os.environ, 'EVERNOOK_WORKER_LIFETIME_PIPE': flag}, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'worker_lifetime_unavailable')

    def test_preexisting_pipe_data_prevents_private_window_start(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'x')
            result = subprocess.run([str(Path(sys.prefix) / 'Scripts/python.exe'), '-m',
                'evernook.private_window_worker', 'service-private-window'], stdin=read_fd,
                env={**os.environ, 'EVERNOOK_WORKER_PARENT_PID': str(os.getpid()), 'EVERNOOK_WORKER_LIFETIME_PIPE': '1'},
                capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)['error']['code'], 'worker_lifetime_unavailable')
        finally:
            os.close(read_fd); os.close(write_fd)

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable')
    def test_node_host_pipe_close_and_crash_terminate_owned_job(self) -> None:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        node = shutil.which('node')
        assert node is not None
        for mode in ('pipe_close', 'pipe_data', 'host_crash'):
            with self.subTest(mode=mode):
                handles: list[int] = []
                with subprocess.Popen([node, str(Path(__file__).with_name('owned_worker_node_fixture.mjs')),
                        sys.executable, str(Path(__file__).with_name('owned_worker_fixture.py')), mode], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW) as parent:
                    try:
                        assert parent.stdout is not None and parent.stdin is not None
                        lines: list[bytes] = []
                        ready = threading.Event()
                        def read_line() -> None:
                            assert parent.stdout is not None
                            lines.append(parent.stdout.readline()); ready.set()
                        threading.Thread(target=read_line, daemon=True).start()
                        self.assertTrue(ready.wait(10))
                        line = lines[0]
                        self.assertTrue(line)
                        for process_id in set(json.loads(line)):
                            handle: int = kernel.OpenProcess(0x100001, False, process_id)
                            self.assertTrue(handle)
                            handles.append(handle)
                        if mode != 'host_crash':
                            parent.stdin.write(b'x'); parent.stdin.flush()
                        else:
                            parent.kill(); parent.wait(timeout=5)
                        for handle in handles:
                            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                        if mode != 'host_crash':
                            self.assertIsNone(parent.poll())
                    finally:
                        if parent.poll() is None:
                            parent.kill()
                        for handle in handles:
                            kernel.TerminateProcess(handle, 1)
                            kernel.CloseHandle(handle)

    def test_owned_worker_cli_returns_normal_success_without_killing_its_caller(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-owned-cli-") as temporary:
            config, _ = initialize(Path(temporary) / "ordinary", client_id="synthetic", entry="synthetic")
            access = config.parent / "mcp-access.json"
            pair_client(config, client_id="synthetic", entry="synthetic", access_path=access)
            result = subprocess.run([str(Path(sys.prefix) / "Scripts/python.exe"), "-m", "evernook.worker", "status",
                "--config", str(config), "--access", str(access)], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])

    def test_private_window_refuses_to_start_when_owner_guard_is_unavailable(self) -> None:
        for operation in ("service-private-window", "service-private-session"):
            with self.subTest(operation=operation):
                result = subprocess.run([str(Path(sys.prefix) / "Scripts/python.exe"), "-m", "evernook.worker", operation],
                    env={**os.environ, "EVERNOOK_WORKER_PARENT_PID": "invalid"}, capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "worker_lifetime_unavailable")


if __name__ == "__main__":
    unittest.main()
