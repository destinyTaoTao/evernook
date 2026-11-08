from __future__ import annotations

from contextlib import closing
from dataclasses import replace
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
                if sql.startswith("SELECT rowid,message_id FROM lexical WHERE message_id IN"):
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
