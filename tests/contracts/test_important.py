from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import sqlite3
import tempfile
import unittest

from evernook import Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize


class ImportantTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-important-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.receipt = self.runtime.submit(self.access, Event(Source("test", "session", "main", "1"),
            "user", 1, None, "complete", "甲🙂e\u0301乙\r\n合成：37.50元。"))

    def test_handshake_exposes_important_capability_and_actual_storage_version(self) -> None:
        capabilities = self.runtime.handshake(self.access)
        self.assertTrue(capabilities["important"])
        self.assertEqual(capabilities["storage_version"], 3)

    def test_explicit_span_is_durable_immediately_and_repeated_mark_keeps_identity(self) -> None:
        mark = ImportantMark(self.receipt.message_id, "original", 1, "synthetic-shared", 1, 4, "🙂e\u0301")
        item = self.runtime.mark_important(self.access, mark)
        self.assertEqual(self.runtime.read_important(self.access, item.important_id).text, "🙂e\u0301")
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.mark_important(self.access, mark), item)
        page = reopened.read_important(self.access, item.important_id)
        self.assertEqual(page.item, item)
        self.assertEqual(page.item.scope, "synthetic-shared")
        self.assertEqual((page.start, page.end, page.remaining, page.next_cursor), (1, 4, 0, None))
        self.assertEqual(reopened.status(self.access).pending_jobs, 1)

    def test_whole_long_message_pages_without_summarizing_or_splitting_utf16_units(self) -> None:
        text = "甲🙂e\u0301乙" * 1000 + "\r\n37.50元。"
        receipt = self.runtime.submit(self.access, Event(Source("test", "session", "main", "long"),
            "user", 2, None, "complete", text))
        item = self.runtime.mark_important(self.access, ImportantMark(receipt.message_id, "original", 1,
            "synthetic-shared", None, None, text))
        first = self.runtime.read_important(self.access, item.important_id)
        self.assertEqual((first.start, first.end, first.next_cursor), (0, 2048, 2048))
        self.assertGreater(first.remaining, 0)
        pieces = [first.text]
        cursor = first.next_cursor
        while cursor is not None:
            page = self.runtime.read_important(self.access, item.important_id, cursor=cursor, limit=4096, content_version=1)
            self.assertEqual(page.start, cursor)
            pieces.append(page.text)
            cursor = page.next_cursor
        self.assertEqual(page.end, item.end)
        self.assertEqual(page.remaining, 0)
        self.assertEqual("".join(pieces), text)

    def test_concurrent_marks_are_idempotent_and_scopes_are_saved_independently(self) -> None:
        mark = ImportantMark(self.receipt.message_id, "original", 1, "synthetic-shared", 1, 4, "🙂e\u0301")
        with ThreadPoolExecutor(max_workers=8) as pool:
            items = list(pool.map(lambda _: self.runtime.mark_important(self.access, mark), range(16)))
        self.assertTrue(all(item == items[0] for item in items))
        self.assertEqual(self.runtime.status(self.access).important_items, 1)
        second = self.runtime.mark_important(self.access, replace(mark, scope="synthetic-project"))
        self.assertNotEqual(second.important_id, items[0].important_id)
        self.assertEqual(self.runtime.status(self.access).important_items, 2)
        self.assertEqual(self.runtime.status(self.access).pending_jobs, 1)

    def test_invalid_references_versions_and_codepoint_spans_are_refused_atomically(self) -> None:
        mark = ImportantMark(self.receipt.message_id, "original", 1, "synthetic-shared", 1, 4, "🙂e\u0301")
        for request, code in [
            (replace(mark, message_id="missing"), "message_not_found"),
            (replace(mark, content_version=2), "version_mismatch"),
            (replace(mark, representation="projection"), "representation_mismatch"),
            (replace(mark, start=-1), "invalid_span"),
            (replace(mark, start=True), "invalid_span"),
            (replace(mark, start=None), "invalid_span"),
            (replace(mark, end=100), "invalid_span"),
            (replace(mark, end=1), "invalid_span"),
            (replace(mark, end=5), "span_mismatch"),  # UTF-16 counting would incorrectly include 乙.
            (replace(mark, expected_text="different"), "span_mismatch"),
        ]:
            with self.assertRaisesRegex(MemoryFailure, "^" + code + "$"):
                self.runtime.mark_important(self.access, request)
        self.assertEqual(self.runtime.status(self.access).important_items, 0)
        item = self.runtime.mark_important(self.access, mark)
        for cursor in [0, 4, -1]:
            with self.assertRaisesRegex(MemoryFailure, "invalid_cursor"):
                self.runtime.read_important(self.access, item.important_id, cursor=cursor)
        for limit in [0, 4097, True]:
            with self.assertRaisesRegex(MemoryFailure, "invalid_page"):
                self.runtime.read_important(self.access, item.important_id, limit=limit)
        with self.assertRaisesRegex(MemoryFailure, "version_mismatch"):
            self.runtime.read_important(self.access, item.important_id, content_version=2)
        with self.assertRaisesRegex(MemoryFailure, "important_not_found"):
            self.runtime.read_important(self.access, "missing")

    def test_sensitive_marks_only_reference_projection_and_do_not_expose_original(self) -> None:
        receipt = self.runtime.submit(self.access, Event(Source("test", "session", "main", "sensitive"),
            "user", 2, None, "complete", "密码：synthetic-only-123"))
        mark = ImportantMark(receipt.message_id, "original", 1, "synthetic-shared", None, None, "密码：synthetic-only-123")
        with self.assertRaisesRegex(MemoryFailure, "representation_mismatch"):
            self.runtime.mark_important(self.access, mark)
        with self.assertRaisesRegex(MemoryFailure, "span_mismatch"):
            self.runtime.mark_important(self.access, replace(mark, representation="projection"))
        safe = replace(mark, representation="projection", expected_text="[PROTECTED:ORIGINAL_NOT_SAVED]")
        item = self.runtime.mark_important(self.access, safe)
        page = self.runtime.read_important(self.access, item.important_id)
        self.assertEqual(page.text, "[PROTECTED:ORIGINAL_NOT_SAVED]")
        self.assertEqual(page.item.representation, "projection")
        self.assertFalse(receipt.original_saved)
        with self.assertRaisesRegex(MemoryFailure, "sensitive_metadata"):
            self.runtime.mark_important(self.access, replace(safe, scope="password=synthetic-scope-123"))
        self.assertEqual(self.runtime.status(self.access).important_items, 1)

    def test_legacy_storage_upgrade_and_failed_background_work_preserve_exact_mark(self) -> None:
        original = self.runtime.read(self.access, self.receipt.message_id)
        assert original is not None
        # System-boundary fixture: the documented T05 schema has no important table and version 1.
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as legacy:
            legacy.execute("DROP TABLE source_context")
            legacy.execute("DROP TABLE visibility")
            legacy.execute("DROP TABLE changes")
            legacy.execute("DROP TABLE important")
            legacy.execute("PRAGMA user_version=1")
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.read(self.access, self.receipt.message_id), original)
        mark = ImportantMark(self.receipt.message_id, "original", 1, "synthetic-shared", None, None, original.event.text)
        item = reopened.mark_important(self.access, mark)
        # Inject a persisted job failure; there is no job consumer in this ticket.
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as failure:
            failure.execute("UPDATE jobs SET state='failed'")
        recovered = MemoryRuntime(self.config)
        self.assertEqual(recovered.status(self.access).pending_jobs, 0)
        self.assertEqual(recovered.read_important(self.access, item.important_id).text, original.event.text)
        self.assertEqual(recovered.mark_important(self.access, mark), item)
        self.assertEqual(recovered.status(self.access).important_items, 1)


if __name__ == "__main__":
    unittest.main()
