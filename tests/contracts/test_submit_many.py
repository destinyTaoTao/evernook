from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Access, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.purge import PurgeDecision, PurgeWorkflow


class SubmitManyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-bulk-test-')
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='synthetic')
        self.runtime = MemoryRuntime(self.config)

    def event(self, index: int, text: str = '合成批量证据37.50🙂') -> Event:
        return Event(Source('synthetic', 'batch', 'main', str(index)), 'user', index, None, 'complete', text)

    def test_committed_receipts_roundtrip_replay_and_safe_projection(self) -> None:
        events = (self.event(0), self.event(1, 'password=synthetic-only-secret-123'), self.event(2))
        receipts = self.runtime.submit_many(self.access, events)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(self.runtime.submit_many(self.access, events), receipts)
        self.assertEqual(self.runtime.submit(self.access, events[0]), receipts[0])
        fresh = MemoryRuntime(self.config)
        self.assertEqual(fresh.read(self.access, receipts[0].message_id).event, events[0])  # type: ignore[union-attr]
        protected = fresh.read(self.access, receipts[1].message_id)
        assert protected is not None
        self.assertFalse(receipts[1].original_saved)
        self.assertNotIn('synthetic-only-secret-123', protected.event.text)
        with fresh._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 3)
            self.assertEqual(core.execute('SELECT count(*) FROM jobs').fetchone()[0], 3)

    def test_conflict_in_middle_rolls_back_all_new_originals_and_jobs(self) -> None:
        original = self.event(1)
        receipt = self.runtime.submit(self.access, original)
        with self.assertRaisesRegex(MemoryFailure, 'source_conflict'):
            self.runtime.submit_many(self.access, (self.event(0), replace(original, text='different'), self.event(2)))
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(core.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertEqual(self.runtime.read(self.access, receipt.message_id).event, original)  # type: ignore[union-attr]

    def test_inflight_cancellation_rolls_back_and_retry_is_idempotent(self) -> None:
        canceled = threading.Event()
        persist = self.runtime._persist_event
        def interrupt(*args: object, **kwargs: object) -> object:
            result = persist(*args, **kwargs)  # type: ignore[arg-type]
            canceled.set()
            return result
        events = (self.event(0), self.event(1))
        with patch.object(self.runtime, '_persist_event', side_effect=interrupt):
            with self.assertRaisesRegex(MemoryFailure, 'submit_canceled'):
                self.runtime.submit_many(self.access, events, cancel=canceled)
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(core.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        canceled.clear()
        receipts = self.runtime.submit_many(self.access, events, cancel=canceled)
        self.assertEqual(self.runtime.submit_many(self.access, events), receipts)

    def test_concurrent_single_and_batch_share_source_identity(self) -> None:
        events = (self.event(0), self.event(1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            bulk = pool.submit(self.runtime.submit_many, self.access, events)
            single = pool.submit(self.runtime.submit, self.access, events[1])
            self.assertEqual(bulk.result(timeout=15)[1], single.result(timeout=15))
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_purged_source_and_storage_failure_cannot_publish_other_batch_items(self) -> None:
        target = self.runtime.submit(self.access, self.event(0))
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, 'synthetic-bulk-purge', (target.message_id,))
        confirmation = self.runtime.submit(self.access, self.event(1, '合成用户确认删除指定测试来源'))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        with self.assertRaisesRegex(MemoryFailure, 'source_purged'):
            self.runtime.submit_many(self.access, (self.event(2), self.event(0)))
        persist = self.runtime._persist_event
        calls: list[bool] = []
        def full(*args: object, **kwargs: object) -> object:
            if calls:
                raise MemoryFailure('storage_full')
            calls.append(True)
            return persist(*args, **kwargs)  # type: ignore[arg-type]
        with patch.object(self.runtime, '_persist_event', side_effect=full):
            with self.assertRaisesRegex(MemoryFailure, 'storage_full'):
                self.runtime.submit_many(self.access, (self.event(2), self.event(3)))
        self.assertIsNone(self.runtime.read_source(self.access, self.event(2).source))
        self.assertIsNone(self.runtime.read_source(self.access, self.event(3).source))

    def test_revoked_capture_before_commit_rolls_back_and_duplicate_items_share_receipt(self) -> None:
        authorize = self.runtime._authorize
        identity = authorize(self.access)
        with patch.object(self.runtime, '_authorize', side_effect=(identity, replace(identity, scopes=frozenset({'recall'})))):
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                self.runtime.submit_many(self.access, (self.event(0), self.event(1)))
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        receipts = self.runtime.submit_many(self.access, (self.event(0), self.event(0)))
        self.assertEqual(receipts[0], receipts[1])
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_cancellation_after_commit_returns_the_committed_receipts(self) -> None:
        canceled = threading.Event()
        connect = sqlite3.connect
        class AfterCommit(sqlite3.Connection):
            saw_original = False
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                result = super().execute(sql, parameters)
                if sql.startswith('INSERT INTO messages'):
                    self.saw_original = True
                if sql == 'COMMIT' and self.saw_original:
                    canceled.set()
                return result
        def open_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return connect(*args, factory=AfterCommit, **kwargs)
        with patch('evernook.runtime.sqlite3.connect', side_effect=open_connection):
            receipts = self.runtime.submit_many(self.access, (self.event(0),), cancel=canceled)
        self.assertTrue(canceled.is_set())
        self.assertEqual(self.runtime.submit(self.access, self.event(0)), receipts[0])

    def test_invalid_oversize_identity_and_cancellation_publish_nothing(self) -> None:
        for events in ((), [self.event(0)], tuple(self.event(i) for i in range(101)),
                       tuple(self.event(i, 'x' * 1048576) for i in range(5))):
            with self.subTest(kind=type(events).__name__, count=len(events)):
                with self.assertRaisesRegex(MemoryFailure, 'invalid_batch_request|batch_too_large'):
                    self.runtime.submit_many(self.access, events)  # type: ignore[arg-type]
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized'):
            self.runtime.submit_many(Access('unknown', 'unknown'), (self.event(0),))
        with self.assertRaisesRegex(MemoryFailure, 'unauthorized_source'):
            self.runtime.submit_many(self.access, (self.event(0), replace(self.event(1), source=Source('other', 'batch', 'main', '1'))))
        canceled = threading.Event(); canceled.set()
        with self.assertRaisesRegex(MemoryFailure, 'submit_canceled'):
            self.runtime.submit_many(self.access, (self.event(0),), cancel=canceled)
        with self.runtime._connection() as core:
            self.assertEqual(core.execute('SELECT count(*) FROM messages').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
