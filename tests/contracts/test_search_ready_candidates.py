from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from threading import Event as ThreadEvent

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize


class ReadyCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-ready-guard-")
        self.addCleanup(temporary.cleanup)
        self.config, self.actor = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "target"), "user", 1,
            "2021-01-01T00:00:00Z", "complete", "合成：紫罗兰书签。")
        self.target = self.runtime.submit(self.actor, self.event)
        self.runtime.refresh_lexical(self.actor)

    def test_selective_ready_query_does_not_scan_every_source_as_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-ready-candidates-") as temporary:
            config, actor = initialize(Path(temporary) / "data", client_id="synthetic", entry="test")
            runtime = MemoryRuntime(config)
            event = Event(Source("test", "session", "main", "target"), "user", 1,
                "2021-01-01T00:00:00Z", "complete", "合成：唯一的紫罗兰书签。")
            target = runtime.submit(actor, event)
            for number in range(200):
                runtime.submit(actor, replace(event, source=replace(event.source, event=f"other-{number}"),
                    sequence=number + 2, text="合成：无关的乌龙茶记录。"))
            self.assertEqual(runtime.refresh_lexical_window(actor, limit=5000).state, "ready")
            expected = runtime.search(actor, "紫罗兰")
            self.assertEqual([hit.message_id for hit in expected.hits], [target.message_id])
            real_connect = sqlite3.connect
            observed: list[int] = []

            class BoundedCandidates(sqlite3.Connection):
                steps = 0

                def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                    if sql.startswith("SELECT messages.event,messages.receipt,"):
                        observed.append(0)

                        def budget() -> int:
                            self.steps += 100
                            observed[-1] = self.steps
                            return int(self.steps > 2000)

                        self.set_progress_handler(budget, 100)
                    return super().execute(sql, parameters)

            def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                return real_connect(*args, **kwargs, factory=BoundedCandidates)

            for accelerated in (False, True):
                with self.subTest(accelerated=accelerated):
                    runtime.configure_lexical_acceleration(actor, enabled=accelerated)
                    with patch("sqlite3.connect", side_effect=connect):
                        self.assertEqual(runtime.search(actor, "紫罗兰"), expected)
            self.assertTrue(observed)
            self.assertLessEqual(max(observed), 2000)
            with closing(sqlite3.connect(config.parent / "memory.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 201)

    def test_large_candidate_sets_keep_ordered_paging_without_expanding_sql_parameters(self) -> None:
        for number in range(5):
            self.runtime.submit(self.actor, replace(self.event,
                source=replace(self.event.source, event=f"popular-{number}"), sequence=number + 2))
        self.runtime.refresh_lexical(self.actor)
        expected = self.runtime.search(self.actor, "紫罗兰", limit=2)
        real_connect = sqlite3.connect

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
            return db

        with patch("sqlite3.connect", side_effect=connect):
            page = self.runtime.search(self.actor, "紫罗兰", limit=2)
        self.assertEqual(page, expected)
        self.assertEqual(page.next_offset, 2)
        self.assertTrue(page.used_index)

    def test_small_candidates_fit_the_actual_connection_parameter_limit(self) -> None:
        self.runtime.submit(self.actor, replace(self.event, source=replace(self.event.source, event="second"), sequence=2))
        self.runtime.refresh_lexical(self.actor)
        expected = self.runtime.search(self.actor, "紫罗兰")
        real_connect = sqlite3.connect

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)
            return db

        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.search(self.actor, "紫罗兰"), expected)
        self.assertEqual(len(expected.hits), 2)

    def test_duplicate_fts_candidates_do_not_duplicate_source_hits(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            for _ in range(4):
                db.execute("INSERT INTO lexical SELECT message_id,terms FROM lexical WHERE rowid=(SELECT min(rowid) FROM lexical)")
        page = self.runtime.search(self.actor, "紫罗兰")
        self.assertEqual([hit.message_id for hit in page.hits], [self.target.message_id])
        self.assertEqual(page.index.state, "ready")

    def test_failed_candidate_probe_returns_complete_original_fallback(self) -> None:
        expected = self.runtime.search(self.actor, "紫罗兰")
        real_connect = sqlite3.connect
        injected: list[bool] = []

        class Fault(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT message_id FROM lexical WHERE lexical MATCH ? LIMIT ?"):
                    injected.append(True)
                    raise sqlite3.OperationalError("synthetic bounded probe failure")
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Fault)

        with patch("sqlite3.connect", side_effect=connect):
            page = self.runtime.search(self.actor, "紫罗兰")
        self.assertEqual(injected, [True])
        self.assertEqual(page.hits, expected.hits)
        self.assertFalse(page.used_index)
        self.assertEqual(page.fallback_reason, "index_unavailable")
        self.assertEqual(self.runtime.search(self.actor, "紫罗兰"), expected)

    def test_canceled_probe_never_returns_a_complete_empty_page(self) -> None:
        real_connect = sqlite3.connect
        cancel = ThreadEvent()

        class Canceled(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT message_id FROM lexical WHERE lexical MATCH ? LIMIT ?"):
                    cancel.set()
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Canceled)

        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "search_canceled"):
                self.runtime.search(self.actor, "紫罗兰", cancel=cancel)
        self.assertTrue(cancel.is_set())

    def test_capture_during_probe_is_not_mixed_into_the_current_snapshot(self) -> None:
        expected = self.runtime.search(self.actor, "紫罗兰")
        real_connect = sqlite3.connect
        inserted: list[bool] = []
        runtime, actor, event = self.runtime, self.actor, self.event

        class Concurrent(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = super().execute(sql, parameters)
                if sql.startswith("SELECT message_id FROM lexical WHERE lexical MATCH ? LIMIT ?") and not inserted:
                    inserted.append(True)
                    runtime.submit(actor, replace(event, source=replace(event.source, event="concurrent"), sequence=2))
                return cursor

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Concurrent)

        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(runtime.search(actor, "紫罗兰"), expected)
        self.assertEqual(inserted, [True])
        current = runtime.search(actor, "紫罗兰")
        self.assertEqual(current.index.state, "behind")
        self.assertEqual(len(current.hits), 2)


if __name__ == "__main__":
    unittest.main()
