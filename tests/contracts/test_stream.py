from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-stream-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.memory = MemoryRuntime(self.config)
        self.text = "合成🙂e\u0301\r\n37.50元。"
        self.event = Event(Source("test", "session", "main", "attempt:1"), "assistant", 1, None, "complete", self.text)
        self.frames = (
            {"type": "block-start", "index": 0, "blockType": "text"},
            {"type": "text-delta", "index": 0, "text": self.text[:3]},
            {"type": "text-delta", "index": 0, "text": self.text[3:]},
            {"type": "block-end", "index": 0, "block": {"type": "text", "text": self.text}},
            {"type": "finish", "reason": {"kind": "stop"}},
        )

    def test_durable_buffered_stream_and_raw_frames_share_idempotent_capture_receipt(self) -> None:
        first = self.memory.submit_stream(self.access, self.event, self.frames)
        self.assertTrue(first.complete)
        self.assertTrue(first.message.original_saved)
        self.assertEqual(self.memory.submit_stream(self.access, self.event, self.frames), first)
        restarted = MemoryRuntime(self.config)
        original = restarted.read(self.access, first.message.message_id)
        assert original is not None
        self.assertEqual(original.event.text, self.text)
        raw = restarted.read_attachment(self.access, first.message.message_id, "raw-stream-v1")
        assert raw is not None
        self.assertEqual(json.loads(raw), list(self.frames))
        altered = (*self.frames[:-1], {"type": "usage", "usage": {"outputTokens": 1}}, self.frames[-1])
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            restarted.submit_stream(self.access, self.event, altered)

    def test_split_tool_credentials_force_projection_and_do_not_store_raw_frame_secrets(self) -> None:
        sentinel = "synthetic-tool-secret-123"
        frames = (
            {"type": "block-start", "index": 0, "blockType": "text"},
            {"type": "text-delta", "index": 0, "text": "合成正常显示"},
            {"type": "tool-call-delta", "index": 1, "argumentsDelta": "pass"},
            {"type": "text-delta", "index": 0, "text": "。"},
            {"type": "tool-call-delta", "index": 1, "argumentsDelta": "word=" + sentinel},
            {"type": "finish", "reason": {"kind": "tool-calls"}},
        )
        receipt = self.memory.submit_stream(self.access, replace(self.event, text="合成正常显示。"), frames)
        self.assertFalse(receipt.message.original_saved)
        self.assertFalse(receipt.attachments[0].saved)
        self.assertIsNone(self.memory.read_attachment(self.access, receipt.message.message_id, "raw-stream-v1"))
        contents = b"".join(path.read_bytes() for path in self.config.parent.iterdir() if path.is_file())
        self.assertNotIn(sentinel.encode(), contents)

    def test_claimed_display_text_missing_finish_wrong_role_and_oversize_are_refused(self) -> None:
        for event, frames, code in (
            (replace(self.event, text="synthetic mismatch"), self.frames, "stream_text_mismatch"),
            (self.event, self.frames[:-1], "incomplete_stream"),
            (replace(self.event, role="user"), self.frames, "invalid_stream"),
            (self.event, ({"type": "text-delta", "index": 0, "text": "x" * 1048576},), "stream_too_large"),
        ):
            with self.assertRaisesRegex(MemoryFailure, code):
                self.memory.submit_stream(self.access, event, frames)
        self.assertEqual(self.memory.status(self.access).messages, 0)

    def test_unfinished_attempt_preserves_explicit_state_without_uncertain_raw_prefix(self) -> None:
        frames = ({"type": "text-delta", "index": 0, "text": "synthetic-prefix-123"},
                  {"type": "finish", "reason": {"kind": "error"}})
        receipt = self.memory.submit_stream(self.access, replace(self.event, completion="failed", text="synthetic-prefix-123"), frames)
        self.assertFalse(receipt.complete)
        self.assertFalse(receipt.message.original_saved)
        self.assertIn("incomplete_output", receipt.gaps)
        self.assertIsNone(self.memory.read_attachment(self.access, receipt.message.message_id, "raw-stream-v1"))


if __name__ == "__main__":
    unittest.main()
