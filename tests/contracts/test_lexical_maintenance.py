from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.lexical import NORMALIZATION_IDENTITY, index_terms


class LexicalMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-lexical-maintenance-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "first"), "user", 1, None, "complete", "合成：原始索引记录。")
        self.first = self.runtime.submit(self.access, self.event)
        self.runtime.refresh_lexical(self.access)

    def test_accelerated_batch_fetches_only_selected_original_bodies(self) -> None:
        large = self.runtime.submit(self.access, replace(self.event,
            source=replace(self.event.source, event="large-later"), sequence=2, text="合成：" + "长" * 10000))
        # A valid earlier receiving clock puts this small source before the large
        # pending original, whose physical row was inserted first.
        with patch("evernook.runtime.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
            small = self.runtime.submit(self.access, replace(self.event,
                source=replace(self.event.source, event="small-earlier"), sequence=3))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
            return db
        # SQLite rejects a fetched oversized body even if it would later be
        # discarded by sorting. Selecting IDs before fetching bodies must avoid it.
        with patch("sqlite3.connect", side_effect=connect):
            state = self.runtime.refresh_lexical(self.access, limit=1)
        self.assertEqual((state.state, state.indexed_messages, state.pending_messages), ("behind", 2, 1))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (small.message_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (large.message_id,)).fetchone())
        original = self.runtime.read(self.access, large.message_id)
        assert original is not None
        self.assertEqual(original.event.text, "合成：" + "长" * 10000)

    def test_accelerated_pending_sql_failure_falls_back_to_complete_selection(self) -> None:
        self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="pending"), sequence=2))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        failures: list[bool] = []
        class FailedSelection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT event,receipt FROM messages JOIN ("):
                    failures.append(True)
                    raise sqlite3.OperationalError("synthetic optional selection failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=FailedSelection)
        with patch("sqlite3.connect", side_effect=connect):
            state = self.runtime.refresh_lexical(self.access)
        self.assertEqual(failures, [True])
        self.assertEqual((state.state, state.indexed_messages), ("ready", 2))

    def test_accelerated_status_sql_failure_uses_original_coverage(self) -> None:
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        expected = self.runtime.lexical_status(self.access)
        real_connect = sqlite3.connect
        failures: list[bool] = []
        class FailedCount(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT count(*),max(messages.received)") and "INDEXED BY evernook_message_cover_v1" in sql:
                    failures.append(True)
                    raise sqlite3.OperationalError("synthetic optional count failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=FailedCount)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.lexical_status(self.access), expected)
        self.assertEqual(failures, [True])

    def test_concurrent_index_removal_keeps_selection_snapshot(self) -> None:
        self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="pending"), sequence=2))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        removed: list[bool] = []
        runtime, access = self.runtime, self.access
        class RemovedIndex(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if sql.startswith("SELECT type,tbl_name,sql FROM sqlite_master") and not removed:
                    removed.append(True)
                    runtime.configure_lexical_acceleration(access, enabled=False)
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=RemovedIndex)
        with patch("sqlite3.connect", side_effect=connect):
            state = self.runtime.refresh_lexical(self.access)
        self.assertEqual(removed, [True])
        self.assertEqual((state.state, state.indexed_messages), ("ready", 2))
        self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))

    def test_lost_selection_transaction_does_not_retry_or_index_sources(self) -> None:
        receipt = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="pending"), sequence=2))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        attempted: list[str] = []
        class LostTransaction(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT event,receipt FROM messages"):
                    attempted.append(sql)
                    if "JOIN (" in sql:
                        super().execute("ROLLBACK")
                        raise sqlite3.OperationalError("synthetic read transaction lost")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=LostTransaction)
        with patch("sqlite3.connect", side_effect=connect):
            state = self.runtime.refresh_lexical(self.access)
        self.assertEqual(len(attempted), 1)
        self.assertEqual((state.state, state.indexed_messages, state.pending_messages), ("unavailable", 1, 1))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (receipt.message_id,)).fetchone())

    def test_accelerated_selection_rechecks_versions_fts_presence_and_duplicate_coverage(self) -> None:
        receipts = [self.first]
        for i in range(4):
            receipts.append(self.runtime.submit(self.access, replace(self.event,
                source=replace(self.event.source, event=f"version-{i}"), sequence=i+2)))
        self.runtime.refresh_lexical(self.access)
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DELETE FROM lexical WHERE message_id=?", (receipts[0].message_id,))
            db.execute("UPDATE lexical_docs SET content_version=content_version+1 WHERE message_id=?", (receipts[1].message_id,))
            db.execute("UPDATE lexical_docs SET normalization_version=-1 WHERE message_id=?", (receipts[2].message_id,))
            db.execute("DELETE FROM lexical_docs WHERE message_id=?", (receipts[3].message_id,))
            db.execute("INSERT INTO lexical SELECT message_id,terms FROM lexical WHERE message_id=?", (receipts[4].message_id,))
        state = self.runtime.refresh_lexical(self.access, limit=2)
        self.assertEqual((state.indexed_messages, state.pending_messages), (3, 2))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical WHERE message_id=?", (receipts[0].message_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT content_version FROM lexical_docs WHERE message_id=?", (receipts[1].message_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT normalization_version FROM lexical_docs WHERE message_id=?", (receipts[2].message_id,)).fetchone()[0], -1)
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (receipts[3].message_id,)).fetchone())
        self.assertEqual(self.runtime.refresh_lexical(self.access, limit=2).state, "ready")

    def test_append_batch_does_not_rescan_existing_fts_for_every_new_document(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.executemany("INSERT INTO lexical VALUES (?,?)", [(self.first.message_id, "synthetic") for _ in range(2000)])
        for i in range(20):
            self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event=f"append-{i}"), sequence=i+2))
        operations: list[bool] = []
        real_connect = sqlite3.connect
        def progress() -> int:
            operations.append(True)
            return 0
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.set_progress_handler(progress, 100)
            return db
        with patch("sqlite3.connect", side_effect=connect):
            state = self.runtime.refresh_lexical(self.access, limit=20)
        self.assertEqual((state.source_messages, state.indexed_messages), (21, 21))
        self.assertLess(len(operations), 800, "A small append batch must not do one whole-FTS scan per new document")

    def test_external_indexer_replacing_missing_rows_invalidates_the_batch_lookup(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DELETE FROM lexical")
        real_terms = index_terms
        other = MemoryRuntime(self.config)
        called: list[bool] = []
        def concurrent(text: str) -> str:
            if not called:
                called.append(True)
                other.refresh_lexical(self.access)
            return real_terms(text)
        with patch("evernook.runtime.index_terms", side_effect=concurrent):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical WHERE message_id=?", (self.first.message_id,)).fetchone()[0], 1)

    def test_foreign_rebuild_and_rowid_reuse_cannot_delete_another_documents_index(self) -> None:
        later = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="later"), sequence=2))
        self.runtime.refresh_lexical(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DELETE FROM lexical")
            db.execute("INSERT INTO lexical(rowid,message_id,terms) VALUES (1,?,?)", (later.message_id, index_terms(self.event.text)))
            db.execute("INSERT INTO lexical(rowid,message_id,terms) VALUES (2,?,?)", (self.first.message_id, index_terms(self.event.text)))
            db.execute("UPDATE lexical_docs SET normalization_version=0 WHERE message_id=?", (later.message_id,))
        real_terms = index_terms
        other = MemoryRuntime(self.config)
        called: list[bool] = []
        def concurrent(text: str) -> str:
            if not called:
                called.append(True)
                other.rebuild_lexical(self.access, limit=1)
            return real_terms(text)
        with patch("evernook.runtime.index_terms", side_effect=concurrent):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT message_id FROM lexical ORDER BY message_id").fetchall(),
                             [(message_id,) for message_id in sorted((self.first.message_id, later.message_id))])
            self.assertEqual(db.execute("SELECT DISTINCT normalization_version FROM lexical_docs").fetchall(), [(NORMALIZATION_IDENTITY,)])

    def test_oversized_lookup_falls_back_without_leaving_duplicate_rows(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("INSERT INTO lexical SELECT message_id,terms FROM lexical")
            db.execute("UPDATE lexical_docs SET normalization_version=0")
        with patch("evernook.runtime._LEXICAL_ROWID_CACHE_LIMIT", 1):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 1)

    def test_lookup_read_failure_uses_the_authoritative_delete_path(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("UPDATE lexical_docs SET normalization_version=0")
        real_connect = sqlite3.connect
        injected: list[bool] = []
        class FailedLookup(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT rowid,message_id FROM lexical WHERE "):
                    injected.append(True)
                    raise sqlite3.OperationalError("synthetic optional lookup failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=FailedLookup)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        self.assertEqual(injected, [True])
        self.assertEqual(self.runtime.search(self.access, "原始索引").hits[0].message_id, self.first.message_id)

    def test_failed_second_document_commit_retains_only_the_completed_prefix(self) -> None:
        first = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="append-first"), sequence=2))
        second = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="append-second"), sequence=3))
        real_connect = sqlite3.connect
        class CommitFailed(sqlite3.Connection):
            inserted: int = 0
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql == "COMMIT" and self.inserted == 2:
                    raise sqlite3.OperationalError("synthetic second document commit failure")
                cursor = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO lexical VALUES"):
                    self.inserted += 1
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=CommitFailed)
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                self.runtime.refresh_lexical(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (first.message_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (second.message_id,)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 3)

    def test_later_malformed_receipt_does_not_preempt_earlier_committed_work(self) -> None:
        first = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="valid-first"), sequence=2))
        second = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="malformed-second"), sequence=3))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("UPDATE messages SET receipt='{synthetic-malformed' WHERE id=?", (second.message_id,))
        with self.assertRaises(ValueError):
            self.runtime.refresh_lexical(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (first.message_id,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (second.message_id,)).fetchone())
