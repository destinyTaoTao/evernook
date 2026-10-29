from __future__ import annotations

from pathlib import Path
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize
from contract_assertions import assert_original_roundtrip


class TextContractTests(unittest.TestCase):
    def test_concurrent_retries_share_receipt_and_job_but_real_duplicate_events_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-concurrent-") as temporary:
            config, access = initialize(Path(temporary) / "data", client_id="synthetic", entry="test")
            runtime = MemoryRuntime(config)
            event = Event(Source("test", "session", "main", "1"), "assistant", 0, None, "partial", "合成片段🙂")
            with ThreadPoolExecutor(max_workers=12) as pool:
                receipts = list(pool.map(lambda _: runtime.submit(access, event), range(24)))
            self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
            self.assertEqual(runtime.status(access).messages, 1)
            self.assertEqual(runtime.status(access).pending_jobs, 1)
            with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
                runtime.submit(access, replace(event, completion="complete"))
            distinct = runtime.submit(access, replace(event, source=replace(event.source, event="2")))
            self.assertNotEqual(distinct.message_id, receipts[0].message_id)
            self.assertEqual(MemoryRuntime(config).status(access).messages, 2)
            self.assertEqual(runtime.status(access).pending_jobs, 2)

    def test_same_time_listing_preserves_source_sequence_with_pagination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-order-") as temporary:
            config, access = initialize(Path(temporary) / "data", client_id="synthetic", entry="test")
            runtime = MemoryRuntime(config)
            for sequence in reversed(range(12)):
                runtime.submit(access, Event(Source("test", "session", "main", str(sequence)), "user", sequence,
                                             "2021-02-03T02:20:30Z", "complete", f"synthetic {sequence}"))
            first = runtime.list_messages(access, limit=7)
            self.assertEqual([item.event.sequence for item in first.messages], list(range(7)))
            self.assertEqual(first.next_offset, 7)
            last = runtime.list_messages(access, limit=7, offset=7)
            self.assertEqual([item.event.sequence for item in last.messages], list(range(7, 12)))
            self.assertIsNone(last.next_offset)
    def test_sensitive_detection_and_declared_sources_store_only_safe_projection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-sensitive-") as temporary:
            config, access = initialize(Path(temporary) / "data", client_id="synthetic-client", entry="synthetic-dsh")
            runtime = MemoryRuntime(config)
            for number, (text, sensitive, protected) in enumerate([
                ("合成密码：example-only-123", False, False),
                ('{"api_key":"synthetic-example-123"}', False, False),
                ("Authorization: Bearer synthetic-example-123", False, False),
                ("opaque synthetic protected content", True, False),
                ("synthetic tool output", False, True),
            ]):
                event = Event(Source("synthetic-dsh", "session", "main", str(number)), "user", number,
                              None, "complete", text, sensitive, protected)
                receipt = runtime.submit(access, event)
                self.assertEqual(receipt.status, "projection_only")
                self.assertFalse(receipt.original_saved)
                stored = MemoryRuntime(config).read(access, receipt.message_id)
                assert stored is not None
                self.assertEqual(stored.event.text, "[PROTECTED:ORIGINAL_NOT_SAVED]")
                self.assertEqual(runtime.submit(access, event), receipt)
                with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
                    runtime.submit(access, replace(event, text=text + "different"))
            for path in config.parent.iterdir():
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(b"synthetic-example-123", data)
                    self.assertNotIn(b"example-only-123", data)

    def test_sensitive_source_metadata_is_refused_without_persisting_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-metadata-") as temporary:
            config, access = initialize(Path(temporary) / "data", client_id="synthetic-client", entry="synthetic-dsh")
            runtime = MemoryRuntime(config)
            event = Event(Source("synthetic-dsh", "password=synthetic-meta-value", "main", "1"),
                          "user", 1, None, "complete", "safe synthetic text")
            with self.assertRaisesRegex(MemoryFailure, "sensitive_metadata"):
                runtime.submit(access, event)
            self.assertEqual(runtime.status(access).messages, 0)
            self.assertEqual(runtime.status(access).pending_jobs, 0)

    def test_committed_unicode_original_is_identical_after_restart_by_id_source_and_time(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-text-") as temporary:
            config, access = initialize(Path(temporary) / "data", client_id="synthetic-client", entry="synthetic-dsh")
            event = Event(Source("synthetic-dsh", "session-1", "main", "user-1"), "user", 1,
                          "2021-02-03T10:20:30+08:00", "complete", "合成：37.50元。\r\n甲🙂e\u0301乙")
            runtime = MemoryRuntime(config)
            receipt = runtime.submit(access, event)
            self.assertEqual(receipt.status, "committed")
            self.assertTrue(receipt.original_saved)
            reopened = MemoryRuntime(config)
            stored = reopened.read(access, receipt.message_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.event, event)
            assert_original_roundtrip(self, event, asdict(receipt), asdict(stored))
            self.assertEqual(reopened.read_source(access, event.source), stored)
            self.assertEqual(reopened.list_messages(access, since="2021-02-03T02:20:30Z", until="2021-02-03T02:20:31Z").messages, (stored,))
            self.assertEqual(reopened.submit(access, event), receipt)
            self.assertEqual(reopened.status(access).messages, 1)
            self.assertEqual(reopened.status(access).pending_jobs, 1)
            self.assertEqual(access.contract_version, 1)


if __name__ == "__main__":
    unittest.main()
