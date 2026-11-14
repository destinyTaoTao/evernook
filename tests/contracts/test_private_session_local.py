from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from evernook import Access, MemoryFailure
from evernook.private_session_local import LocalPrivateSession, LocalPrivateSessionWindow
from evernook.private_capture_local import LocalPrivateCapture
from test_private_capture_local import FakeClient


class PrivateSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.access = Access('synthetic', 'token')
        self.session = LocalPrivateSession(self.client, self.access, 'synthetic')

    def test_retry_is_frozen_then_distinct_entries_share_session(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, 'private_capture_unavailable'):
            self.session.submit('synthetic-private-first')
        with self.assertRaisesRegex(MemoryFailure, 'private_capture_content_changed'):
            self.session.submit('synthetic-private-changed')
        self.session.submit('synthetic-private-first')
        self.session.submit('synthetic-private-second')
        events = self.client.events
        self.assertEqual(asdict(events[0]), asdict(events[1]))
        self.assertEqual(events[1].source.session, events[2].source.session)
        self.assertNotEqual(events[1].source.event, events[2].source.event)
        self.assertEqual([events[1].sequence, events[2].sequence], [0, 1])
        self.assertTrue(all(event.sensitive for event in events))
        summary = self.session.close()
        self.assertEqual(summary['saved_count'], 2)
        self.assertEqual(summary['state'], 'saved')
        self.assertNotIn('synthetic-private', json.dumps(summary))

    def test_close_during_write_retains_unknown_identifier_and_no_new_write(self) -> None:
        entered, release = threading.Event(), threading.Event()
        original = self.client.protected_receive
        self.client.fail = False
        def blocked(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            entered.set()
            if not release.wait(5):
                raise RuntimeError('synthetic timeout')
            return original(*args, **kwargs)  # type: ignore[arg-type]
        self.client.protected_receive = blocked  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.session.submit, 'synthetic-private-pending')
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(MemoryFailure, 'private_session_busy'):
                self.session.submit('another')
            summary = self.session.close()
            self.assertEqual(summary['state'], 'outcome_unknown')
            self.assertEqual(summary['sequence'], 0)
            self.assertNotIn('synthetic-private', json.dumps(summary))
            release.set()
            future.result(timeout=5)
        self.assertEqual(self.session.close(), summary)
        with self.assertRaisesRegex(MemoryFailure, 'private_session_closed'):
            self.session.submit('another')

    def test_close_after_save_before_session_ack_counts_saved_once(self) -> None:
        self.client.fail = False
        saved, release = threading.Event(), threading.Event()
        original = LocalPrivateCapture.submit
        def pause_after(capture: LocalPrivateCapture, text: str) -> None:
            original(capture, text)
            saved.set()
            if not release.wait(5):
                raise RuntimeError('synthetic timeout')
        with patch.object(LocalPrivateCapture, 'submit', pause_after), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.session.submit, 'synthetic-private-saved')
            self.assertTrue(saved.wait(5))
            result = self.session.close()
            self.assertEqual(result['state'], 'saved')
            self.assertEqual(result['saved_count'], 1)
            release.set()
            future.result(timeout=5)
        self.assertEqual(self.session.close(), result)

    def test_resume_requires_explicit_ids_and_preserves_unknown_on_cancel(self) -> None:
        with self.assertRaises(MemoryFailure):
            LocalPrivateSession(self.client, self.access, 'synthetic', sequence=1)
        with self.assertRaises(MemoryFailure):
            self.session.submit('synthetic-private-retry')
        info = self.session.close()
        resumed = LocalPrivateSession(self.client, self.access, 'synthetic', session_id=info['session_id'],
                                      pending_event_id=info['pending_event_id'], sequence=info['sequence'])
        self.assertEqual(resumed.close()['state'], 'outcome_unknown')
        retry = LocalPrivateSession(self.client, self.access, 'synthetic', session_id=info['session_id'],
                                    pending_event_id=info['pending_event_id'], sequence=info['sequence'])
        retry.submit('synthetic-private-retry')
        self.assertEqual(asdict(self.client.events[0]), asdict(self.client.events[-1]))
        self.assertEqual(retry.close()['saved_count'], 1)

    def test_limit_never_sends_an_extra_record(self) -> None:
        self.client.fail = False
        session = LocalPrivateSession(self.client, self.access, 'synthetic',
            session_id='00000000-0000-4000-8000-000000000001',
            pending_event_id='00000000-0000-4000-8000-000000000002', sequence=99)
        session.submit('synthetic-last')
        with self.assertRaisesRegex(MemoryFailure, 'private_session_limit'):
            session.submit('synthetic-over-limit')
        self.assertEqual(len(self.client.events), 1)

    def test_hidden_tk_clears_each_record_and_allows_next(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        root.withdraw()
        self.client.fail = False
        with patch('evernook.private_session_local.input_desktop_available', return_value=True):
            window = LocalPrivateSessionWindow(root, self.session)
            try:
                for secret in ('synthetic-private-first', 'synthetic-private-second'):
                    window.text.insert('1.0', secret)
                    window._submit()
                    deadline = time.monotonic() + 5
                    while str(window.save['state']) != 'normal' and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    self.assertEqual(window.text.get('1.0', 'end-1c'), '')
                    self.assertEqual(str(window.save['state']), 'normal')
                window.close()
                self.assertEqual(self.session.close()['saved_count'], 2)
            finally:
                if not window.closed:
                    window.close()
