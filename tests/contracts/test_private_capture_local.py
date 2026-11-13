from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import threading
import time
import tkinter as tk
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Access, Event, MemoryFailure
from evernook.private_capture_local import LocalPrivateCapture, LocalPrivateWindow


class FakeClient:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.fail = True

    def protected_receive(self, access: Access, event: Event, *, timeout: float = 30) -> dict[str, Any]:
        self.events.append(event)
        if self.fail:
            self.fail = False
            raise RuntimeError('synthetic-private-ui-sentinel')
        return {'state': 'protected_linked', 'encrypted_original_saved': True,
                'object_id': '00000000-0000-4000-8000-000000000001',
                'inbox_id': '00000000-0000-4000-8000-000000000002',
                'message': {'original_saved': False, 'message_id': '00000000-0000-4000-8000-000000000003'}}


class PrivateCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.capture = LocalPrivateCapture(self.client, Access('synthetic', 'token'), 'synthetic')

    def test_failed_attempt_freezes_content_and_retries_same_event(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, '^private_capture_unavailable$') as failure:
            self.capture.submit('synthetic-private-ui-sentinel')
        self.assertNotIn('synthetic-private-ui-sentinel', str(failure.exception))
        with self.assertRaisesRegex(MemoryFailure, '^private_capture_content_changed$'):
            self.capture.submit('different private note')
        self.capture.submit('synthetic-private-ui-sentinel')
        self.assertEqual(asdict(self.client.events[0]), asdict(self.client.events[1]))
        result = self.capture.close()
        self.assertEqual(result['state'], 'saved')
        self.assertNotIn('synthetic-private-ui-sentinel', json.dumps(result))
        with self.assertRaisesRegex(MemoryFailure, '^private_capture_closed$'):
            self.capture.submit('synthetic-private-ui-sentinel')

    def test_close_during_inflight_submission_reports_uncertain(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def blocked(access: Access, event: Event, *, timeout: float = 30) -> dict[str, Any]:
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture timeout')
            return {'state': 'protected_linked', 'encrypted_original_saved': True,
                    'object_id': '00000000-0000-4000-8000-000000000001',
                    'inbox_id': '00000000-0000-4000-8000-000000000002',
                    'message': {'original_saved': False, 'message_id': '00000000-0000-4000-8000-000000000003'}}
        self.client.protected_receive = blocked  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.capture.submit, 'synthetic-private-ui-sentinel')
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(MemoryFailure, '^private_capture_busy$'):
                self.capture.submit('synthetic-private-ui-sentinel')
            result = self.capture.close()
            self.assertEqual(result['state'], 'outcome_unknown')
            release.set()
            future.result(timeout=5)
        self.assertEqual(self.capture.close()['state'], 'outcome_unknown')

    def test_new_window_can_retry_same_source_after_uncertain_close(self) -> None:
        with self.assertRaises(MemoryFailure):
            self.capture.submit('synthetic-private-ui-sentinel')
        saved = self.capture.close()
        again = LocalPrivateCapture(self.client, Access('synthetic', 'token'), 'synthetic',
                                    session_id=saved['session_id'], event_id=saved['event_id'])
        again.submit('synthetic-private-ui-sentinel')
        self.assertEqual(asdict(self.client.events[0]), asdict(self.client.events[1]))
        self.assertEqual(again.close()['state'], 'saved')

    def test_real_tk_window_retries_frozen_content_and_clears_on_close(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        root.withdraw()
        with patch('evernook.private_capture_local.input_desktop_available', return_value=True):
            window = LocalPrivateWindow(root, self.capture)
            try:
                window.text.insert('1.0', 'synthetic-private-ui-sentinel')
                window._submit()
                deadline = time.monotonic() + 5
                while str(window.save['state']) != 'normal' and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(str(window.save['state']), 'normal')
                self.assertEqual(str(window.text['state']), 'disabled')
                window._submit()
                deadline = time.monotonic() + 5
                while str(window.status['text']) != '已加密保存。' and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(str(window.status['text']), '已加密保存。')
                self.assertEqual(window.text.get('1.0', 'end-1c'), '')
                self.assertEqual(asdict(self.client.events[0]), asdict(self.client.events[1]))
            finally:
                window.close()

    def test_focus_moving_from_window_to_text_does_not_close_capture(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        with patch('evernook.private_capture_local.input_desktop_available', return_value=True):
            window = LocalPrivateWindow(root, self.capture)
            try:
                root.focus_force()
                root.update()
                window.text.focus_force()
                root.update()
                window._focus_lost(type('FocusEvent', (), {'widget': root})())
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(window.closed)
            finally:
                if not window.closed:
                    window.close()

    def test_focus_leaving_window_clears_and_closes(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        with patch('evernook.private_capture_local.input_desktop_available', return_value=True):
            window = LocalPrivateWindow(root, self.capture)
            try:
                window.text.insert('1.0', 'synthetic-private-ui-sentinel')
                root.focus_force()
                root.update()
                other = tk.Toplevel(root)
                root.update()
                with patch.object(window.text, 'delete', wraps=window.text.delete) as clear:
                    other.focus_force()
                    deadline = time.monotonic() + 2
                    while not window.closed and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                    self.assertTrue(window.closed)
                    clear.assert_called_with('1.0', 'end')
                self.assertEqual(self.capture.close()['state'], 'canceled')
            finally:
                if not window.closed:
                    window.close()


if __name__ == '__main__':
    unittest.main()
