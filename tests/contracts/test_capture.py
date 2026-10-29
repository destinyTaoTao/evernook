from __future__ import annotations

import hashlib
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from evernook import AttachmentInput, Capture, Change, Event, MemoryFailure, MemoryRuntime, Source, initialize


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-capture-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.input = self.runtime.submit(self.access, Event(Source("test", "session", "branch-a", "input"),
            "user", 1, None, "complete", "synthetic tool request"))

    def test_tool_result_and_multiblock_attachment_commit_together_and_roundtrip_after_restart(self) -> None:
        data = "合成附件\r\n甲🙂e\u0301乙37.50元。".encode("utf-8")
        attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (data[:8], data[8:]),
                                     len(data), hashlib.sha256(data).hexdigest())
        event = Event(Source("test", "session", "branch-a", "tool-1"), "tool", 2, None, "complete", "synthetic tool result")
        capture = Capture(event, "tool_result", self.input.message_id, "synthetic-tool", (attachment,))
        receipt = self.runtime.submit_capture(self.access, capture)
        self.assertTrue(receipt.complete)
        self.assertEqual(receipt.gaps, ())
        self.assertTrue(receipt.message.original_saved)
        self.assertTrue(receipt.attachments[0].saved)
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.read_capture(self.access, receipt.message.message_id), receipt)
        self.assertEqual(reopened.read_attachment(self.access, receipt.message.message_id, "a1"), data)
        self.assertEqual(reopened.submit_capture(self.access, capture), receipt)
        stored = reopened.read(self.access, receipt.message.message_id)
        assert stored is not None
        self.assertEqual(stored.event, event)
        self.assertEqual(reopened.status(self.access).messages, 2)
        self.assertEqual(reopened.status(self.access).pending_jobs, 2)

    def test_sensitive_multiblock_attachment_and_protected_tool_source_never_store_originals(self) -> None:
        for number, (body, protected_source) in enumerate([("synthetic ordinary body", False),
                                                          ("synthetic protected body", True)]):
            data = b"password=synthetic-secret-only-123"
            attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (data[:4], data[4:11], data[11:]))
            event = Event(Source("test", "session", "branch-a", f"sensitive-{number}"),
                "tool", 2, None, "complete", body, protected_source=protected_source)
            receipt = self.runtime.submit_capture(self.access,
                Capture(event, "tool_result", self.input.message_id, "synthetic-tool", (attachment,)))
            self.assertFalse(receipt.complete)
            self.assertEqual(receipt.attachments[0].gap, "protected_original_not_saved")
            self.assertIsNone(receipt.attachments[0].sha256)
            self.assertIsNone(self.runtime.read_attachment(self.access, receipt.message.message_id, "a1"))
            if protected_source:
                self.assertFalse(receipt.message.original_saved)
        for path in self.config.parent.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-secret-only-123", path.read_bytes())

    def test_unsupported_missing_partial_corrupt_and_oversize_inputs_report_queryable_gaps(self) -> None:
        base = AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic attachment",))
        cases = [
            (replace(base, media_type="application/pdf", chunks=(b"%PDF-synthetic",)), "unsupported_format"),
            (replace(base, chunks=None), "entry_not_provided"),
            (replace(base, complete=False), "incomplete_attachment"),
            (replace(base, chunks=(b"\xff",)), "invalid_encoding"),
            (replace(base, chunks=(b"x" * 1048577,)), "size_limit"),
            (replace(base, expected_size=99), "integrity_mismatch"),
            (replace(base, expected_sha256="0" * 64), "integrity_mismatch"),
        ]
        for number, (attachment, gap) in enumerate(cases):
            event = Event(Source("test", "session", "branch-a", f"gap-{number}"), "tool", 2,
                          None, "partial", "synthetic partial result")
            receipt = self.runtime.submit_capture(self.access,
                Capture(event, "tool_result", self.input.message_id, "synthetic-tool", (attachment,)))
            self.assertFalse(receipt.complete)
            self.assertIn("incomplete_output", receipt.gaps)
            self.assertEqual(receipt.attachments[0].gap, gap)
            self.assertFalse(receipt.attachments[0].saved)
            self.assertIsNone(receipt.attachments[0].sha256)
            self.assertIsNone(self.runtime.read_attachment(self.access, receipt.message.message_id, "a1"))
            self.assertEqual(self.runtime.read_capture(self.access, receipt.message.message_id), receipt)
        event = Event(Source("test", "session", "branch-a", "missing-body"), "tool", 3,
                      None, "complete", "")
        missing = self.runtime.submit_capture(self.access, Capture(event, "tool_result", self.input.message_id,
                                                                   "synthetic-tool", provided=False))
        self.assertIn("entry_not_provided", missing.gaps)
        self.assertFalse(missing.message.original_saved)

    def test_chunked_sensitive_and_unfinished_streams_are_never_persisted_in_plaintext(self) -> None:
        for number, (chunks, completion) in enumerate([
            (("pass", "word=synthetic-stream-secret-123"), "complete"),
            (("pass",), "partial"),
            (("synthetic unfinished output",), "failed"),
        ]):
            event = Event(Source("test", "session", "branch-a", f"stream-{number}"), "tool", 2, None, completion, "")
            capture = Capture(event, "tool_result", self.input.message_id, "synthetic-tool", text_chunks=chunks)
            receipt = self.runtime.submit_capture(self.access, capture)
            self.assertFalse(receipt.message.original_saved)
            self.assertFalse(receipt.complete)
            stored = self.runtime.read(self.access, receipt.message.message_id)
            assert stored is not None
            self.assertEqual(stored.event.text, "[PROTECTED:ORIGINAL_NOT_SAVED]")
            self.assertEqual(self.runtime.submit_capture(self.access, capture), receipt)
        ordinary = Capture(Event(Source("test", "session", "branch-a", "stream-ordinary"), "tool", 2, None,
            "complete", ""), "tool_result", self.input.message_id, "synthetic-tool", text_chunks=("甲🙂", "e\u0301乙"))
        receipt = self.runtime.submit_capture(self.access, ordinary)
        stored = self.runtime.read(self.access, receipt.message.message_id)
        assert stored is not None
        self.assertTrue(receipt.complete)
        self.assertEqual(stored.event.text, "甲🙂e\u0301乙")
        for path in self.config.parent.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-stream-secret-123", path.read_bytes())

    def test_concurrent_retries_conflicts_and_missing_link_do_not_orphan_attachments(self) -> None:
        attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic file",))
        capture = Capture(Event(Source("test", "session", "branch-a", "call-1"), "assistant", 2,
            None, "complete", "synthetic tool input"), "tool_call", self.input.message_id, "synthetic-tool", (attachment,))
        with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
            self.runtime.submit_capture(self.access, replace(capture, input_id="missing"))
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.runtime.submit_capture(self.access, capture), range(16)))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(self.runtime.status(self.access).messages, 2)
        self.assertEqual(self.runtime.status(self.access).pending_jobs, 2)
        for request in [replace(capture, tool_name="different"),
                        replace(capture, attachments=(replace(attachment, chunks=(b"different",)),))]:
            with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
                self.runtime.submit_capture(self.access, request)
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            self.runtime.submit(self.access, capture.event)
        self.assertEqual(self.runtime.read_attachment(self.access, receipts[0].message.message_id, "a1"), b"synthetic file")
        branch = self.runtime.submit_capture(self.access, replace(capture,
            event=replace(capture.event, source=replace(capture.event.source, branch="branch-b"))))
        self.assertNotEqual(branch.message.message_id, receipts[0].message.message_id)
        self.assertEqual(self.runtime.status(self.access).messages, 3)
        stored = self.runtime.read(self.access, branch.message.message_id)
        assert stored is not None
        self.assertEqual(stored.event.source.branch, "branch-b")

    def test_attachment_damage_or_missing_object_is_visible_and_never_reported_as_complete(self) -> None:
        attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic original",))
        capture = Capture(Event(Source("test", "session", "branch-a", "damaged"), "tool", 2,
            None, "complete", "synthetic tool result"), "tool_result", self.input.message_id, "synthetic-tool", (attachment,))
        receipt = self.runtime.submit_capture(self.access, capture)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("UPDATE attachments SET content=?", (b"corrupt synthetic",))
        with self.assertRaisesRegex(MemoryFailure, "attachment_corrupt"):
            self.runtime.read_attachment(self.access, receipt.message.message_id, "a1")
        damaged = self.runtime.read_capture(self.access, receipt.message.message_id)
        assert damaged is not None
        self.assertFalse(damaged.complete)
        self.assertEqual(damaged.attachments[0].gap, "attachment_corrupt")
        self.assertFalse(self.runtime.submit_capture(self.access, capture).complete)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("DELETE FROM attachments")
        missing = self.runtime.read_capture(self.access, receipt.message.message_id)
        assert missing is not None
        self.assertFalse(missing.complete)
        self.assertEqual(missing.attachments[0].gap, "attachment_missing")

    def test_ledger_linkage_failure_rolls_back_message_and_attachment_then_retry_succeeds(self) -> None:
        attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic file",))
        capture = Capture(Event(Source("test", "session", "branch-a", "link-failure"), "tool", 2,
            None, "complete", "synthetic result"), "tool_result", self.input.message_id, "synthetic-tool", (attachment,))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON attachments BEGIN SELECT RAISE(ABORT,'synthetic attachment failure'); END")
        with self.assertRaisesRegex(MemoryFailure, "^storage_unavailable$"):
            self.runtime.submit_capture(self.access, capture)
        reopened = MemoryRuntime(self.config)
        self.assertIsNone(reopened.read_source(self.access, capture.event.source))
        self.assertEqual(reopened.status(self.access).messages, 1)
        self.assertEqual(reopened.status(self.access).pending_jobs, 1)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as fault:
            fault.execute("DROP TRIGGER synthetic_failure")
        receipt = reopened.submit_capture(self.access, capture)
        self.assertEqual(reopened.read_attachment(self.access, receipt.message.message_id, "a1"), b"synthetic file")

    def test_invalid_envelope_and_body_limits_are_explicit_and_suppression_blocks_attachment_reads(self) -> None:
        event = Event(Source("test", "session", "branch-a", "invalid"), "tool", 2, None, "complete", "synthetic")
        attachment = AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic file",))
        capture = Capture(event, "tool_result", self.input.message_id, "synthetic-tool", (attachment,))
        for request, code in [
            (replace(capture, input_id=None), "invalid_capture"),
            (replace(capture, tool_name=None), "invalid_capture"),
            (replace(capture, kind="unknown"), "invalid_capture"),
            (replace(capture, attachments=(attachment, attachment)), "duplicate_attachment"),
            (replace(capture, attachments=(replace(attachment, expected_size=True),)), "invalid_attachment"),
            (replace(capture, attachments=(replace(attachment, label="password=synthetic"),)), "sensitive_metadata"),
        ]:
            with self.assertRaisesRegex(MemoryFailure, code):
                self.runtime.submit_capture(self.access, request)
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        for number, (body, gap) in enumerate([("x" * 1048577, "size_limit"), ("bad\ud800", "invalid_encoding")]):
            receipt = self.runtime.submit_capture(self.access, replace(capture,
                event=replace(event, source=replace(event.source, event=f"body-{number}"), text=body)))
            self.assertIn(gap, receipt.gaps)
            self.assertFalse(receipt.message.original_saved)
        receipt = self.runtime.submit_capture(self.access, capture)
        self.runtime.change(self.access, Change("synthetic-suppress", receipt.message.message_id, 1,
            "suppress", self.input.message_id, "synthetic reason"))
        self.assertIsNone(self.runtime.read_capture(self.access, receipt.message.message_id))
        self.assertIsNone(self.runtime.read_attachment(self.access, receipt.message.message_id, "a1"))
