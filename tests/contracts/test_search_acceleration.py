from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from threading import Event as ThreadEvent
from typing import Any
import unittest
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
import test_search


class AcceleratedSearchTests(test_search.SearchTests):
    def setUp(self) -> None:
        super().setUp()
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)


class SearchAccelerationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-search-acceleration-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "1"), "user", 1,
            "2021-02-03T10:20:30+08:00", "complete", "合成：乌龙茶37.50元。🙂")
        self.receipt = self.runtime.submit(self.access, self.event)
        self.runtime.refresh_lexical(self.access)

    def test_opt_in_persists_without_automatic_startup_migration_and_is_reversible(self) -> None:
        expected = self.runtime.search(self.access, "乌龙")
        self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))
        self.assertFalse(MemoryRuntime(self.config).lexical_acceleration_enabled(self.access))
        self.assertTrue(self.runtime.configure_lexical_acceleration(self.access, enabled=True))
        self.assertTrue(self.runtime.configure_lexical_acceleration(self.access, enabled=True))
        reopened = MemoryRuntime(self.config)
        self.assertTrue(reopened.lexical_acceleration_enabled(self.access))
        self.assertEqual(reopened.search(self.access, "乌龙"), expected)
        self.assertFalse(reopened.configure_lexical_acceleration(self.access, enabled=False))
        self.assertFalse(reopened.configure_lexical_acceleration(self.access, enabled=False))
        self.assertEqual(reopened.search(self.access, "乌龙"), expected)
        original = reopened.read(self.access, self.receipt.message_id)
        assert original is not None
        self.assertEqual(original.event.text, self.event.text)

    def test_harness_cannot_create_or_remove_the_persistent_index(self) -> None:
        actor = pair_client(self.config, client_id="harness", entry="harness",
            access_path=self.config.parent / "mcp-access-harness.json", scopes=("capture", "recall"))
        for enabled in (True, False):
            with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                self.runtime.configure_lexical_acceleration(actor, enabled=enabled)
        self.assertFalse(self.runtime.lexical_acceleration_enabled(actor))

    def test_non_boolean_configuration_cannot_enable_the_index(self) -> None:
        value: Any
        for value in (1, "true", None):
            with self.assertRaisesRegex(MemoryFailure, "invalid_lexical_acceleration"):
                self.runtime.configure_lexical_acceleration(self.access, enabled=value)
        self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))

    def test_conflicting_index_definition_is_not_used_or_overwritten(self) -> None:
        expected = self.runtime.search(self.access, "乌龙")
        for name in ("evernook_message_cover_v1", "EVERNOOK_MESSAGE_COVER_V1"):
            with self.subTest(name=name):
                with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
                    db.execute(f"CREATE INDEX {name} ON messages(received)")
                self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))
                self.assertEqual(self.runtime.search(self.access, "乌龙"), expected)
                for enabled in (True, False):
                    with self.assertRaisesRegex(MemoryFailure, "lexical_acceleration_conflict"):
                        self.runtime.configure_lexical_acceleration(self.access, enabled=enabled)
                with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
                    self.assertIn("messages(received)", db.execute(
                        "SELECT sql FROM sqlite_master WHERE name=? COLLATE NOCASE", (name,)).fetchone()[0])
                    db.execute(f"DROP INDEX {name}")

    def test_temp_failure_falls_back_to_the_same_complete_page(self) -> None:
        expected = self.runtime.search(self.access, "乌龙")
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        injected: list[bool] = []
        class Fault(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("INSERT INTO temp.evernook_search_valid_v1"):
                    injected.append(True)
                    raise sqlite3.OperationalError("synthetic optional temp failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Fault)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.search(self.access, "乌龙"), expected)
        self.assertEqual(injected, [True])

    def test_accelerated_search_uses_connection_local_memory_and_no_later_temp_state(self) -> None:
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        stores: list[int] = []
        membership: list[bool] = []
        class Observed(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO temp.evernook_search_valid_v1"):
                    stores.append(int(super().execute("PRAGMA temp_store").fetchone()[0]))
                if "messages.id NOT IN (SELECT message_id FROM temp.evernook_search_valid_v1)" in sql:
                    membership.append(True)
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Observed)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(len(self.runtime.search(self.access, "乌龙").hits), 1)
        self.assertEqual(stores, [2])
        self.assertEqual(membership, [True])
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                db.execute("SELECT * FROM temp.evernook_search_valid_v1")

    def test_cancel_during_temp_materialization_never_becomes_a_complete_fallback(self) -> None:
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        cancel = ThreadEvent()
        class Canceled(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("INSERT INTO temp.evernook_search_valid_v1"):
                    cancel.set()
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Canceled)
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "search_canceled"):
                self.runtime.search(self.access, "不存在", cancel=cancel)
        self.assertTrue(cancel.is_set())
        self.assertEqual(len(self.runtime.search(self.access, "乌龙").hits), 1)

    def test_failed_maintenance_commit_leaves_the_previous_index_state(self) -> None:
        expected = self.runtime.search(self.access, "乌龙")
        real_connect = sqlite3.connect
        class CommitFailed(sqlite3.Connection):
            changed_index: bool = False
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql == "COMMIT" and self.changed_index:
                    raise sqlite3.OperationalError("synthetic index commit failure")
                cursor = super().execute(sql, parameters)
                if sql.startswith(("CREATE INDEX evernook_message_cover_v1", "DROP INDEX evernook_message_cover_v1")):
                    self.changed_index = True
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=CommitFailed)
        for enabled in (True, False):
            before = self.runtime.lexical_acceleration_enabled(self.access)
            with patch("sqlite3.connect", side_effect=connect):
                with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                    self.runtime.configure_lexical_acceleration(self.access, enabled=enabled)
            self.assertEqual(self.runtime.lexical_acceleration_enabled(self.access), before)
            self.assertEqual(self.runtime.search(self.access, "乌龙"), expected)
            self.runtime.configure_lexical_acceleration(self.access, enabled=True)

    def test_concurrent_capture_cannot_mix_new_source_counts_into_an_old_snapshot(self) -> None:
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        inserted: list[bool] = []
        event = replace(self.event, source=replace(self.event.source, event="concurrent"), sequence=2)
        runtime, actor = self.runtime, self.access
        class Concurrent(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO temp.evernook_search_valid_v1") and not inserted:
                    inserted.append(True)
                    runtime.submit(actor, event)
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Concurrent)
        with patch("sqlite3.connect", side_effect=connect):
            old = self.runtime.search(self.access, "乌龙")
        self.assertEqual(inserted, [True])
        self.assertEqual((old.index.source_messages, old.index.indexed_messages, len(old.hits)), (1, 1, 1))
        current = self.runtime.search(self.access, "乌龙")
        self.assertEqual((current.index.source_messages, current.index.indexed_messages, len(current.hits)), (2, 1, 2))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='evernook_search_valid_v1'").fetchone())

    def test_concurrent_index_removal_keeps_the_current_read_snapshot_and_next_fallback(self) -> None:
        expected = self.runtime.search(self.access, "乌龙")
        self.runtime.configure_lexical_acceleration(self.access, enabled=True)
        real_connect = sqlite3.connect
        removed: list[bool] = []
        runtime, actor = self.runtime, self.access
        class Removed(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO temp.evernook_search_valid_v1") and not removed:
                    removed.append(True)
                    runtime.configure_lexical_acceleration(actor, enabled=False)
                return cursor
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Removed)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.search(self.access, "乌龙"), expected)
        self.assertEqual(removed, [True])
        self.assertFalse(self.runtime.lexical_acceleration_enabled(self.access))
        self.assertEqual(self.runtime.search(self.access, "乌龙"), expected)
