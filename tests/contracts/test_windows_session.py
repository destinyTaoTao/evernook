from __future__ import annotations

import ctypes
import tkinter as tk
from typing import Any, Callable
import unittest
from unittest.mock import patch

from evernook import Access
from evernook import windows_session
from evernook.private_capture_local import LocalPrivateCapture, LocalPrivateWindow
from evernook.private_session_local import LocalPrivateSession, LocalPrivateSessionWindow
from evernook.vault_local import _load_user32, _user32, input_desktop_available
from test_private_capture_local import FakeClient


class _Function:
    def __init__(self, action: Callable[..., Any]) -> None:
        self.action = action
        self.argtypes = None
        self.restype = None
    def __call__(self, *args: Any) -> Any:
        return self.action(*args)


class SessionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        windows_session._apis.cache_clear()
        windows_session._kernel.cache_clear()
        windows_session._wts.cache_clear()
        self.addCleanup(windows_session._apis.cache_clear)
        self.addCleanup(windows_session._kernel.cache_clear)
        self.addCleanup(windows_session._wts.cache_clear)

    def info(self, *, level: int = 1, session: int = 7, state: int = 0, flags: int = 1) -> bytes:
        info = windows_session._InfoPrefix()
        info.level = level
        info.data.level_one.session_id = session
        info.data.level_one.state = state
        info.data.level_one.flags = flags
        return bytes(info)

    def test_only_matching_active_unlocked_session_is_accepted(self) -> None:
        self.assertTrue(windows_session._valid_info(self.info(), 7))
        for raw, expected in ((self.info(level=2), 7), (self.info(session=8), 7), (self.info(state=4), 7),
                              (self.info(flags=0), 7), (self.info(flags=-1), 7), (self.info()[:-1], 7)):
            self.assertFalse(windows_session._valid_info(raw, expected))

    def test_query_failures_and_short_buffers_fail_closed_and_release_memory(self) -> None:
        backing = ctypes.create_string_buffer(self.info())
        freed: list[int] = []
        outcome = {'query': True, 'size': len(backing.raw), 'process': True}
        def process(_pid, output):  # type: ignore[no-untyped-def]
            ctypes.cast(output, ctypes.POINTER(ctypes.c_ulong))[0] = 7
            return outcome['process']
        def query(_server, _session, _kind, output, size):  # type: ignore[no-untyped-def]
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(backing)
            ctypes.cast(size, ctypes.POINTER(ctypes.c_ulong))[0] = int(outcome['size'])
            return outcome['query']
        class Kernel:
            GetCurrentProcessId = _Function(lambda: 123)
            ProcessIdToSessionId = _Function(process)
        class Wts:
            WTSQuerySessionInformationW = _Function(query)
            WTSFreeMemory = _Function(lambda pointer: freed.append(pointer.value))
        with patch('evernook.windows_session.ctypes.WinDLL', side_effect=lambda name, **_: Kernel() if name == 'kernel32' else Wts()) as loader, \
             patch('evernook.windows_session.sys.getwindowsversion', return_value=(10, 0, 0, 0, '')):
            self.assertTrue(windows_session.current_session_unlocked())
            outcome['size'] = 1
            self.assertFalse(windows_session.current_session_unlocked())
            outcome['query'] = False
            self.assertFalse(windows_session.current_session_unlocked())
            self.assertEqual(len(freed), 3)
            self.assertEqual(loader.call_count, 2)
            outcome['process'] = False
            self.assertFalse(windows_session.current_session_unlocked())
            self.assertEqual(len(freed), 3)
        with patch('evernook.windows_session.sys.getwindowsversion', return_value=(6, 1, 0, 0, '')):
            self.assertFalse(windows_session.current_session_unlocked())

    def test_input_windows_clear_and_close_before_send_if_state_becomes_unavailable(self) -> None:
        for session_mode in (False, True):
            try:
                root = tk.Tk()
            except tk.TclError:
                self.skipTest('Tk display unavailable')
            root.withdraw()
            client = FakeClient()
            access = Access('synthetic', 'token')
            if session_mode:
                session = LocalPrivateSession(client, access, 'synthetic')
                window: Any = LocalPrivateSessionWindow(root, session)
                module = 'evernook.private_session_local.input_desktop_available'
            else:
                capture = LocalPrivateCapture(client, access, 'synthetic')
                window = LocalPrivateWindow(root, capture)
                module = 'evernook.private_capture_local.input_desktop_available'
            window.text.insert('1.0', 'synthetic-session-secret')
            with patch(module, return_value=False), patch.object(window.text, 'delete', wraps=window.text.delete) as clear:
                window._submit()
                self.assertTrue(window.closed)
                clear.assert_called_with('1.0', 'end')
            self.assertEqual(client.events, [])

    def test_live_session_probe_is_read_only(self) -> None:
        self.assertIsInstance(windows_session.current_session_unlocked(), bool)

    def test_unverified_32_bit_layout_fails_closed(self) -> None:
        with patch('evernook.windows_session.ctypes.sizeof', return_value=4):
            self.assertFalse(windows_session.current_session_unlocked())

    def test_input_desktop_api_exception_fails_closed(self) -> None:
        with patch('evernook.windows_session.current_session_unlocked', return_value=True), \
             patch('evernook.vault_local._user32', side_effect=OSError('synthetic failure')):
            self.assertFalse(input_desktop_available())

    def test_desktop_library_is_loaded_once_across_polls(self) -> None:
        class User:
            OpenInputDesktop = _Function(lambda *_: 0)
            GetUserObjectInformationW = _Function(lambda *_: 0)
            CloseDesktop = _Function(lambda *_: 1)
        _user32.cache_clear()
        _load_user32.cache_clear()
        self.addCleanup(_user32.cache_clear)
        self.addCleanup(_load_user32.cache_clear)
        with patch('evernook.windows_session.current_session_unlocked', return_value=True), \
             patch('evernook.vault_local.ctypes.WinDLL', return_value=User()) as loader:
            self.assertFalse(input_desktop_available())
            self.assertFalse(input_desktop_available())
            self.assertEqual(loader.call_count, 1)

    def test_later_wts_load_failure_does_not_reload_kernel(self) -> None:
        loaded: list[str] = []
        def load(name: str, **_: object) -> object:
            loaded.append(name)
            if name == 'wtsapi32':
                raise OSError('synthetic missing WTS')
            return object()
        with patch('evernook.windows_session.ctypes.WinDLL', side_effect=load), \
             patch('evernook.windows_session.sys.getwindowsversion', return_value=(10, 0, 0, 0, '')):
            self.assertFalse(windows_session.current_session_unlocked())
            self.assertFalse(windows_session.current_session_unlocked())
        self.assertEqual(loaded.count('kernel32'), 1)
        self.assertEqual(loaded.count('wtsapi32'), 2)
