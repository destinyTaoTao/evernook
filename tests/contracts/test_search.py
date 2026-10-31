from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent
from typing import Any
from unittest.mock import patch
import sqlite3
import tempfile
import unittest

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-search-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "1"), "user", 1,
                           "2021-02-03T10:20:30+08:00", "complete", "甲🙂e\u0301乙，乌龙茶37.50元。")
        self.receipt = self.runtime.submit(self.access, self.event)

    def test_short_chinese_number_and_emoji_exact_queries_return_verified_source_spans(self) -> None:
        for query, start, end in [("茶", 8, 9), ("乌龙", 6, 8), ("乌龙茶", 6, 9), ("37.50", 9, 14), ("🙂", 1, 2)]:
            page = self.runtime.search(self.access, query, mode="exact", since="2021-02-03T02:20:30Z", until="2021-02-03T02:20:31Z")
            self.assertEqual(len(page.hits), 1)
            hit = page.hits[0]
            self.assertEqual((hit.message_id, hit.source, hit.role, hit.completion),
                             (self.receipt.message_id, self.event.source, "user", "complete"))
            self.assertEqual((hit.representation, hit.content_version), ("original", 1))
            self.assertEqual((hit.spans[0].start, hit.spans[0].end, hit.spans[0].text), (start, end, query))
            self.assertEqual(self.event.text[start:end], hit.spans[0].text)
            self.assertEqual(page.index.pending_messages, 1)
            self.assertFalse(page.used_index)
        self.assertEqual(self.runtime.search(self.access, "不存在", mode="exact").hits, ())

    def test_lexical_words_and_compatibility_normalization_map_to_original_codepoints(self) -> None:
        original = "合成 Alice 喜欢乌龙茶，金额３７．５０元；甲🙂e\u0301乙，Straße。"
        receipt = self.runtime.submit(self.access, Event(Source("test", "session", "main", "normalized"),
            "user", 2, None, "complete", original))
        cases = [("ALICE 乌龙", "lexical", [(3, 8, "Alice"), (11, 13, "乌龙")]),
                 ("37.50", "exact", [(17, 22, "３７．５０")]),
                 ("é", "exact", [(26, 28, "e\u0301")]),
                 ("STRASSE", "exact", [(30, 36, "Straße")]),
                 ("ss", "exact", [(34, 35, "ß")])]
        for query, mode, expected in cases:
            page = self.runtime.search(self.access, query, mode=mode)
            hit = next(hit for hit in page.hits if hit.message_id == receipt.message_id)
            self.assertEqual([(span.start, span.end, span.text) for span in hit.spans], expected)
            for span in hit.spans:
                self.assertEqual(original[span.start:span.end], span.text)
        self.assertEqual(self.runtime.search(self.access, "ALICE 不存在").hits, ())
        self.assertEqual(self.runtime.search(self.access, "ALICE 乌龙", mode="exact").hits, ())

    def test_index_refresh_lag_and_rebuild_preserve_complete_exact_evidence_path(self) -> None:
        before = self.runtime.search(self.access, "茶")
        ready = self.runtime.refresh_lexical(self.access, limit=1)
        self.assertEqual((ready.state, ready.source_messages, ready.indexed_messages, ready.pending_messages), ("ready", 1, 1, 0))
        self.assertEqual(ready.source_watermark, self.receipt.received_at)
        self.assertEqual(ready.indexed_watermark, self.receipt.received_at)
        indexed = self.runtime.search(self.access, "茶")
        self.assertTrue(indexed.used_index)
        self.assertIsNone(indexed.fallback_reason)
        self.assertEqual(indexed.hits, before.hits)
        second = self.runtime.submit(self.access, Event(Source("test", "session", "main", "new"),
            "user", 2, None, "complete", "合成：另一杯乌龙茶。"))
        lagging = self.runtime.search(self.access, "乌龙")
        self.assertEqual(lagging.index.state, "behind")
        self.assertEqual(lagging.index.pending_messages, 1)
        self.assertEqual(lagging.fallback_reason, "index_behind")
        self.assertEqual({hit.message_id for hit in lagging.hits}, {self.receipt.message_id, second.message_id})
        rebuilt = self.runtime.rebuild_lexical(self.access, limit=1)
        self.assertEqual((rebuilt.indexed_messages, rebuilt.pending_messages), (1, 1))
        self.assertEqual(self.runtime.search(self.access, "乌龙").hits, lagging.hits)
        self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        self.assertEqual(MemoryRuntime(self.config).search(self.access, "乌龙").hits, lagging.hits)
        self.assertEqual(self.runtime.status(self.access).pending_jobs, 2)

    def test_query_symbols_are_literals_and_source_time_and_pages_do_not_silently_drop_history(self) -> None:
        literals = ["合成 OR 1=1; --", '合成 "乌龙"*', "合成 NEAR(乌龙 茶)", "合成 100%_done"]
        receipts = []
        for number, text in enumerate(literals):
            receipts.append(self.runtime.submit(self.access, Event(Source("test", "session", "main", f"literal-{number}"),
                "user", number + 2, "2020-01-01T00:00:00Z", "complete", text)))
        for indexed in [False, True]:
            if indexed:
                self.runtime.refresh_lexical(self.access)
            for text, receipt in zip(literals, receipts, strict=True):
                page = self.runtime.search(self.access, text.removeprefix("合成 "), mode="exact")
                self.assertEqual([hit.message_id for hit in page.hits], [receipt.message_id])
                self.assertEqual(page.hits[0].spans[0].text, text.removeprefix("合成 "))
            self.assertEqual(self.runtime.search(self.access, "乌龙 OR 不存在").hits, ())
            self.assertEqual(self.runtime.search(self.access, "合成", since="2021-01-01T00:00:00Z").hits, ())
            first = self.runtime.search(self.access, "合成", limit=2)
            self.assertEqual(first.next_offset, 2)
            second = self.runtime.search(self.access, "合成", limit=2, offset=2)
            self.assertIsNone(second.next_offset)
            self.assertEqual({hit.message_id for hit in (*first.hits, *second.hits)}, {receipt.message_id for receipt in receipts})
        self.assertEqual(self.runtime.status(self.access).messages, 5)

    def test_correction_and_suppression_apply_before_indexing_and_survive_rebuild(self) -> None:
        self.runtime.refresh_lexical(self.access)
        correction = self.runtime.submit(self.access, Event(Source("test", "session", "main", "correct"),
            "user", 2, None, "complete", "合成更正：乌龙茶38.50元。"))
        self.runtime.change(self.access, Change("synthetic-correct", self.receipt.message_id, 1, "correct", correction.message_id, "synthetic correction"))
        self.assertEqual(self.runtime.search(self.access, "37.50").hits, ())
        fresh = self.runtime.search(self.access, "38.50")
        self.assertEqual([hit.message_id for hit in fresh.hits], [correction.message_id])
        self.assertEqual(fresh.fallback_reason, "index_behind")
        evidence = self.runtime.submit(self.access, Event(Source("test", "session", "main", "suppress"),
            "user", 3, None, "complete", "合成：不再召回更正后的记录。"))
        self.runtime.change(self.access, Change("synthetic-suppress", correction.message_id, 1, "suppress", evidence.message_id, "synthetic suppression"))
        self.assertEqual(self.runtime.search(self.access, "茶").hits, ())
        revoked = self.runtime.submit(self.access, Event(Source("test", "session", "main", "revoke-target"),
            "user", 4, None, "complete", "合成：红茶19.75元。"))
        instruction = self.runtime.submit(self.access, Event(Source("test", "session", "main", "revoke-instruction"),
            "user", 5, None, "complete", "合成：撤销上一条记录。"))
        self.runtime.refresh_lexical(self.access)
        self.assertEqual([hit.message_id for hit in self.runtime.search(self.access, "19.75").hits], [revoked.message_id])
        self.runtime.change(self.access, Change("synthetic-revoke", revoked.message_id, 1, "revoke", instruction.message_id, "synthetic revocation"))
        self.assertEqual(self.runtime.search(self.access, "19.75").hits, ())
        self.runtime.rebuild_lexical(self.access)
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.search(self.access, "乌龙茶").hits, ())
        self.assertEqual(reopened.search(self.access, "19.75").hits, ())
        self.assertEqual(reopened.lexical_status(self.access).source_messages, 2)

    def test_deleted_or_stale_derived_index_returns_exact_fallback_with_truthful_coverage(self) -> None:
        self.runtime.refresh_lexical(self.access)
        expected = self.runtime.search(self.access, "乌龙").hits
        # Inject loss of derived metadata/FTS rows at the database boundary; originals stay untouched.
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("UPDATE lexical_docs SET normalization_version=0")
        stale = self.runtime.search(self.access, "乌龙")
        self.assertEqual(stale.index.pending_messages, 1)
        self.assertEqual(stale.hits, expected)
        self.runtime.refresh_lexical(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("DELETE FROM lexical")
        missing = self.runtime.search(self.access, "乌龙")
        self.assertEqual((missing.index.state, missing.index.pending_messages), ("behind", 1))
        self.assertEqual(missing.hits, expected)
        self.assertEqual(self.runtime.rebuild_lexical(self.access).state, "ready")
        self.assertEqual(self.runtime.search(self.access, "乌龙").hits, expected)

    def test_index_identity_pins_the_interpreter_unicode_database(self) -> None:
        from evernook.lexical import NORMALIZATION_IDENTITY

        self.runtime.refresh_lexical(self.access)
        expected = self.runtime.search(self.access, "乌龙").hits
        self.assertEqual(NORMALIZATION_IDENTITY // 1_000_000, 1)
        self.assertGreater(NORMALIZATION_IDENTITY % 1_000_000, 0)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            self.assertEqual(fault.execute("SELECT DISTINCT normalization_version FROM lexical_docs").fetchall(),
                             [(NORMALIZATION_IDENTITY,)])
            # A store indexed under a different interpreter Unicode database reports behind…
            fault.execute("UPDATE lexical_docs SET normalization_version=1")
        self.assertEqual(self.runtime.lexical_status(self.access).state, "behind")
        # …while the exact fallback still serves complete evidence until the rebuild heals it.
        self.assertEqual(self.runtime.search(self.access, "乌龙").hits, expected)
        self.runtime.refresh_lexical(self.access)
        self.assertEqual(self.runtime.lexical_status(self.access).state, "ready")

    def test_invalid_or_sensitive_queries_are_refused_before_search_and_projection_coordinates_stay_separate(self) -> None:
        for query, code in [("", "invalid_query"), (" " * 5, "invalid_query"), ("x" * 257, "invalid_query"),
                            ("bad\ud800", "invalid_encoding"), ("password=synthetic-private", "protected_query")]:
            with self.assertRaisesRegex(MemoryFailure, "^" + code + "$"):
                self.runtime.search(self.access, query)
        for limit in [0, 101, True]:
            with self.assertRaisesRegex(MemoryFailure, "invalid_page"):
                self.runtime.search(self.access, "茶", limit=limit)
        with self.assertRaisesRegex(MemoryFailure, "invalid_time_range"):
            self.runtime.search(self.access, "茶", since="2021-01-01T00:00:00Z", until="2020-01-01T00:00:00Z")
        protected = self.runtime.submit(self.access, Event(Source("test", "session", "main", "protected"),
            "user", 2, None, "complete", "synthetic-private-original-123", sensitive=True))
        self.runtime.refresh_lexical(self.access)
        self.assertEqual(self.runtime.search(self.access, "synthetic-private-original-123").hits, ())
        page = self.runtime.search(self.access, "PROTECTED")
        self.assertEqual([hit.message_id for hit in page.hits], [protected.message_id])
        hit = page.hits[0]
        self.assertEqual((hit.representation, hit.content_version), ("projection", 1))
        self.assertEqual((hit.spans[0].start, hit.spans[0].end, hit.spans[0].text), (1, 10, "PROTECTED"))

    def test_unavailable_fts_dependency_does_not_block_exact_reads_or_new_originals(self) -> None:
        self.runtime.refresh_lexical(self.access)
        expected = self.runtime.search(self.access, "乌龙").hits
        real_connect = sqlite3.connect

        def deny_index(action: int, table: str | None, column: str | None, database: str | None, trigger: str | None) -> int:
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table is not None and table.startswith("lexical") else sqlite3.SQLITE_OK

        def unavailable_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            connection.set_authorizer(deny_index)
            return connection

        with patch("sqlite3.connect", unavailable_connect):
            fallback = self.runtime.search(self.access, "乌龙")
            self.assertEqual(fallback.hits, expected)
            self.assertFalse(fallback.used_index)
            self.assertEqual((fallback.index.state, fallback.fallback_reason), ("unavailable", "index_unavailable"))
            self.assertIsNotNone(self.runtime.read(self.access, self.receipt.message_id))
            second = self.runtime.submit(self.access, replace(self.event,
                source=replace(self.event.source, event="during-index-failure"), text="合成：绿茶。"))
            self.assertEqual([hit.message_id for hit in self.runtime.search(self.access, "绿茶").hits], [second.message_id])
        self.assertEqual(self.runtime.rebuild_lexical(self.access).state, "ready")
        self.assertEqual(self.runtime.search(self.access, "乌龙").hits, expected)

    def test_index_watermark_cannot_hide_an_earlier_unprocessed_source(self) -> None:
        later = self.runtime.submit(self.access, replace(self.event,
            source=replace(self.event.source, event="later"), text="合成：绿茶。"))
        self.runtime.refresh_lexical(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("UPDATE lexical_docs SET normalization_version=0 WHERE message_id=?", (self.receipt.message_id,))
        status = self.runtime.lexical_status(self.access)
        self.assertEqual((status.state, status.indexed_messages, status.pending_messages), ("behind", 1, 1))
        self.assertEqual(status.source_watermark, later.received_at)
        self.assertEqual(status.indexed_watermark, later.received_at)
        self.assertEqual([hit.message_id for hit in self.runtime.search(self.access, "37.50").hits], [self.receipt.message_id])

    def test_fixed_chinese_names_english_numbers_and_date_corpus_before_and_after_indexing(self) -> None:
        original = "张小北在杭州用EverNook，预算1234.56元，日期2020-05-20。"
        receipt = self.runtime.submit(self.access, Event(Source("test", "session", "main", "corpus"),
            "user", 2, "2020-05-20T09:00:00+08:00", "complete", original))
        cases = [("张", 0, 1, "张"), ("小北", 1, 3, "小北"), ("张小北", 0, 3, "张小北"),
                 ("杭州", 4, 6, "杭州"), ("EVERNOOK", 7, 15, "EverNook"),
                 ("1234.56", 18, 25, "1234.56"), ("2020-05-20", 29, 39, "2020-05-20")]
        for indexed in [False, True]:
            if indexed:
                self.runtime.refresh_lexical(self.access)
            for query, start, end, expected in cases:
                page = self.runtime.search(self.access, query, mode="exact", since="2020-05-20T01:00:00Z", until="2020-05-20T01:00:01Z")
                self.assertEqual([hit.message_id for hit in page.hits], [receipt.message_id])
                span = page.hits[0].spans[0]
                self.assertEqual((span.start, span.end, span.text), (start, end, expected))
                self.assertEqual(original[span.start:span.end], expected)
            both = self.runtime.search(self.access, "张小北 杭州")
            self.assertEqual([(span.start, span.end) for span in both.hits[0].spans], [(0, 3), (4, 6)])
        stored = self.runtime.read(self.access, receipt.message_id)
        assert stored is not None
        self.assertEqual(stored.event.text, original)

    def test_inflight_index_snapshot_cannot_revive_a_source_suppressed_before_commit(self) -> None:
        evidence = self.runtime.submit(self.access, Event(Source("test", "session", "main", "suppress-inflight"),
            "user", 2, None, "complete", "合成：不再召回先前消息。"))
        captured, resume = ThreadEvent(), ThreadEvent()
        real_connect = sqlite3.connect

        class PausingCursor(sqlite3.Cursor):
            def fetchall(self) -> list[Any]:
                rows = super().fetchall()
                names = [column[0] for column in self.description or ()]
                if not captured.is_set() and names == ["event", "receipt"]:
                    captured.set()
                    if not resume.wait(timeout=10):
                        raise TimeoutError("synthetic snapshot was not resumed")
                return rows

        class PausingConnection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                cursor = self.cursor(factory=PausingCursor)
                cursor.execute(sql, parameters)
                return cursor

        def pausing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            kwargs["factory"] = PausingConnection
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            return connection

        with patch("sqlite3.connect", pausing_connect), ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.runtime.refresh_lexical, self.access)
            self.assertTrue(captured.wait(timeout=10))
            try:
                self.runtime.change(self.access, Change("synthetic-inflight", self.receipt.message_id, 1,
                    "suppress", evidence.message_id, "synthetic suppression"))
            finally:
                resume.set()
            self.assertEqual(worker.result(timeout=10).state, "ready")
        self.assertEqual(self.runtime.search(self.access, "乌龙茶").hits, ())
        self.assertEqual(self.runtime.lexical_status(self.access).source_messages, 1)
        self.runtime.rebuild_lexical(self.access)
        self.assertEqual(MemoryRuntime(self.config).search(self.access, "37.50").hits, ())


if __name__ == "__main__":
    unittest.main()
