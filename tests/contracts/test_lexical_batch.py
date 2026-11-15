from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.lexical import index_terms


class LexicalBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-lexical-batch-')
        self.addCleanup(temporary.cleanup)
        config, self.access = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='fixture')
        self.runtime = MemoryRuntime(config)
        self.ids = tuple(item.message_id for item in self.runtime.submit_many(self.access, tuple(
            Event(Source('fixture', 'synthetic', 'main', str(index)), 'user', index, None, 'complete',
                  f'合成词法批次记录{index}') for index in range(12))))

    def test_failed_derivation_keeps_completed_batches_and_retry_rebuilds_exactly(self) -> None:
        calls = 0
        def fail(text: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 7:
                raise RuntimeError('synthetic derivation failure')
            return index_terms(text)
        with patch('evernook.runtime.index_terms', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'synthetic derivation failure'):
                self.runtime.refresh_lexical_window(self.access, limit=12, transaction_size=5)
        self.assertEqual(self.runtime.lexical_status(self.access).indexed_messages, 5)
        self.assertEqual(self.runtime.refresh_lexical_window(self.access, transaction_size=100).indexed_messages, 12)
        self.assertEqual(len(self.runtime.search(self.access, '合成', limit=100).hits), 12)

    def test_source_changed_during_derivation_is_rechecked_inside_commit(self) -> None:
        changed = False
        def suppress(text: str) -> str:
            nonlocal changed
            if not changed:
                changed = True
                self.runtime.change(self.access, Change('synthetic-hide', self.ids[0], 1, 'suppress', self.ids[1], 'synthetic'))
            return index_terms(text)
        with patch('evernook.runtime.index_terms', side_effect=suppress):
            state = self.runtime.refresh_lexical_window(self.access, transaction_size=100)
        self.assertEqual((state.source_messages, state.indexed_messages), (11, 11))
        self.assertNotIn(self.ids[0], [item.message_id for item in self.runtime.search(self.access, '合成', limit=100).hits])
        for invalid in (0, 101, True):
            with self.assertRaises(MemoryFailure):
                self.runtime.refresh_lexical_window(self.access, transaction_size=invalid)

    def test_own_audit_does_not_force_full_fts_delete_scans_per_source(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        statements: list[str] = []
        original = self.runtime._connection
        @contextmanager
        def traced() -> Iterator[sqlite3.Connection]:
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.runtime, '_connection', traced):
            self.assertEqual(self.runtime.refresh_lexical_window(self.access, transaction_size=100).indexed_messages, 12)
        self.assertFalse(any(sql.startswith('DELETE FROM lexical WHERE message_id=') for sql in statements))

    def test_current_checkpoint_selects_dirty_pending_without_full_source_sort(self) -> None:
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        self.runtime.refresh_lexical_window(self.access, transaction_size=100)
        self.runtime.configure_lexical_checkpoint(self.access, enabled=True)
        self.runtime.submit(self.access, Event(Source('fixture', 'synthetic', 'main', 'later'), 'user', 13, None,
                                               'complete', '合成后续词法批次'))
        statements: list[str] = []
        original = self.runtime._connection
        @contextmanager
        def traced() -> Iterator[sqlite3.Connection]:
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.runtime, '_connection', traced):
            self.assertEqual(self.runtime.refresh_lexical_window(self.access, transaction_size=100).indexed_messages, 13)
        self.assertFalse(any('SELECT id FROM messages INDEXED BY' in ' '.join(sql.split()) for sql in statements))
