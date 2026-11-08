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

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.lexical import index_terms


class LexicalWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-lexical-window-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name)/"data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "window", "main", "first"), "user", 1, None, "complete", "合成：窗口维护原文。")
        self.first = self.runtime.submit(self.access, self.event)
        self.runtime.refresh_lexical(self.access)

    def append(self, count: int) -> list[str]:
        return [self.runtime.submit(self.access, replace(self.event,
            source=replace(self.event.source, event=f"append-{i}"), sequence=i+2)).message_id for i in range(count)]

    def test_selected_window_spans_chunks_preserves_order_and_legacy_limit(self) -> None:
        ids = self.append(205)
        status = self.runtime.refresh_lexical_window(self.access, limit=101, budget_seconds=120)
        self.assertEqual((status.source_messages, status.indexed_messages, status.pending_messages), (206, 102, 104))
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            indexed = {row[0] for row in db.execute("SELECT message_id FROM lexical_docs")}
        self.assertEqual(indexed, {self.first.message_id, *ids[:101]})
        self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        self.assertEqual(self.runtime.refresh_lexical_window(self.access).state, "ready")
        with self.assertRaisesRegex(MemoryFailure, "invalid_page"):
            self.runtime.refresh_lexical(self.access, limit=101)
        for message_id in ids:
            stored = self.runtime.read(self.access, message_id)
            assert stored is not None
            self.assertEqual(stored.event.text, self.event.text)

    def test_limits_budgets_and_capture_only_identity_cannot_index_pending_sources(self) -> None:
        self.append(1)
        bad: Any
        for bad in (True, 0, -1, 5001, 1.5, "100"):
            with self.assertRaisesRegex(MemoryFailure, "invalid_page"):
                self.runtime.refresh_lexical_window(self.access, limit=bad)
        for bad in (True, 0, -1, 121, float("nan"), float("inf"), 10**1000, "10"):
            with self.assertRaisesRegex(MemoryFailure, "invalid_maintenance_budget"):
                self.runtime.refresh_lexical_window(self.access, budget_seconds=bad)
        ordinary = pair_client(self.config, client_id="ordinary", entry="ordinary",
            access_path=self.config.parent/"mcp-access-ordinary.json", scopes=("capture", "recall"))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.runtime.refresh_lexical_window(ordinary)
        self.assertEqual(self.runtime.lexical_status(self.access).indexed_messages, 1)

    def test_soft_budget_stops_before_next_write_and_next_window_resumes(self) -> None:
        ids = self.append(2)
        clock = [0.0]
        derived: list[bool] = []
        def terms(text: str) -> str:
            derived.append(True)
            if len(derived) == 2:
                clock[0] = 1.0
            return index_terms(text)
        with patch("evernook.runtime.time.monotonic", side_effect=lambda: clock[0]), patch("evernook.runtime.index_terms", side_effect=terms):
            status = self.runtime.refresh_lexical_window(self.access, budget_seconds=.5)
        self.assertEqual((status.indexed_messages, status.pending_messages), (2, 1))
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[0],)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[1],)).fetchone())
        self.assertEqual(self.runtime.refresh_lexical_window(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 3)

    def test_maintenance_scope_is_rechecked_before_the_next_hundred_sources(self) -> None:
        ids = self.append(101)
        access_path = self.config.parent/"mcp-access-maintenance.json"
        actor = pair_client(self.config, client_id="maintenance", entry="maintenance", access_path=access_path, scopes=("maintain", "recall"))
        derived: list[bool] = []
        def revoke(text: str) -> str:
            derived.append(True)
            if len(derived) == 100:
                pair_client(self.config, client_id="maintenance", entry="maintenance", access_path=access_path, scopes=("recall",))
            return index_terms(text)
        with patch("evernook.runtime.index_terms", side_effect=revoke):
            with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                self.runtime.refresh_lexical_window(actor)
        self.assertEqual(len(derived), 100)
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 101)
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[-1],)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 102)

    def test_suppressed_source_in_a_later_chunk_is_not_revived(self) -> None:
        evidence = self.runtime.submit(self.access, replace(self.event,
            source=replace(self.event.source, event="evidence"), sequence=10000, text="合成：停止召回该来源。"))
        self.runtime.refresh_lexical(self.access)
        ids = self.append(101)
        derived: list[bool] = []
        def suppress(text: str) -> str:
            derived.append(True)
            if len(derived) == 100:
                self.runtime.change(self.access, Change("suppress-later-window", ids[-1], 1, "suppress", evidence.message_id, "synthetic"))
            return index_terms(text)
        with patch("evernook.runtime.index_terms", side_effect=suppress):
            status = self.runtime.refresh_lexical_window(self.access)
        self.assertEqual((status.state, status.source_messages, status.indexed_messages), ("ready", 102, 102))
        self.assertEqual(len(derived), 100)
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[-1],)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 103)

    def test_bad_receipt_in_later_chunk_preserves_the_committed_prefix(self) -> None:
        ids = self.append(101)
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3", isolation_level=None)) as db:
            receipt = db.execute("SELECT receipt FROM messages WHERE id=?", (ids[-1],)).fetchone()[0]
            db.execute("UPDATE messages SET receipt='{synthetic-malformed' WHERE id=?", (ids[-1],))
        with self.assertRaises(ValueError):
            self.runtime.refresh_lexical_window(self.access)
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3", isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical_docs").fetchone()[0], 101)
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[-1],)).fetchone())
            db.execute("UPDATE messages SET receipt=? WHERE id=?", (receipt, ids[-1]))
        self.assertEqual(self.runtime.refresh_lexical_window(self.access).indexed_messages, 102)

    def test_peer_same_version_indexing_does_not_leave_duplicate_fts_rows(self) -> None:
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3", isolation_level=None)) as db:
            db.execute("DELETE FROM lexical")
        other = MemoryRuntime(self.config)
        called: list[bool] = []
        def peer(text: str) -> str:
            if not called:
                called.append(True)
                other.refresh_lexical(self.access)
            return index_terms(text)
        with patch("evernook.runtime.index_terms", side_effect=peer):
            self.assertEqual(self.runtime.refresh_lexical_window(self.access).state, "ready")
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 1)

    def test_second_document_commit_failure_keeps_first_and_resumes(self) -> None:
        ids = self.append(2)
        real_connect = sqlite3.connect
        class FailedCommit(sqlite3.Connection):
            inserted: int = 0
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql == "COMMIT" and self.inserted == 2:
                    raise sqlite3.OperationalError("synthetic second commit failure")
                cursor = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO lexical VALUES"):
                    self.inserted += 1
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs, factory=FailedCommit)
            return connection
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                self.runtime.refresh_lexical_window(self.access)
        with closing(sqlite3.connect(self.config.parent/"memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[0],)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM lexical_docs WHERE message_id=?", (ids[1],)).fetchone())
        self.assertEqual(self.runtime.refresh_lexical_window(self.access).indexed_messages, 3)

    def test_optional_id_selection_sql_failure_falls_back_in_the_same_snapshot(self) -> None:
        self.append(1)
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        failed: list[bool] = []
        class FailedRead(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT id FROM messages INDEXED BY"):
                    failed.append(True)
                    raise sqlite3.OperationalError("synthetic optional ID read failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs, factory=FailedRead)
            return connection
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.refresh_lexical_window(self.access).indexed_messages, 2)
        self.assertEqual(failed, [True])

    def test_lost_selection_transaction_is_not_retried(self) -> None:
        self.append(1)
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        attempted: list[str] = []
        class LostRead(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT id FROM messages"):
                    attempted.append(sql)
                    if "INDEXED BY" in sql:
                        super().execute("ROLLBACK")
                        raise sqlite3.OperationalError("synthetic selection transaction lost")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs, factory=LostRead)
            return connection
        with patch("sqlite3.connect", side_effect=connect):
            status = self.runtime.refresh_lexical_window(self.access)
        self.assertEqual(len(attempted), 1)
        self.assertEqual((status.state, status.indexed_messages, status.pending_messages), ("unavailable", 1, 1))

    def test_only_selected_bodies_are_fetched(self) -> None:
        self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="large"), sequence=2, text="合成："+"长"*10000))
        with patch("evernook.runtime.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
            self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="earlier"), sequence=3))
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
            return connection
        with patch("sqlite3.connect", side_effect=connect):
            status = self.runtime.refresh_lexical_window(self.access, limit=1)
        self.assertEqual((status.indexed_messages, status.pending_messages), (2, 1))
