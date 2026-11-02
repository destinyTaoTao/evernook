from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evernook import Capture, Change, Event, MemoryFailure, MemoryRuntime, PurgeDecision, PurgeWorkflow, Source, initialize


class DurableStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-stream-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.input = self.runtime.submit(self.access, Event(Source("test", "s", "b", "input"), "user", 0, None, "complete", "合成请求"))
        self.event = Event(Source("test", "s", "b", "stream"), "assistant", 1, None, "partial", "")

    def test_acknowledged_chunks_replay_in_order_and_complete_exactly_after_restart(self) -> None:
        first = replace(self.event, text="合成37.50元🙂\r\ne\u0301")
        receipt = self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id)
        self.assertEqual(receipt.state, "open")
        self.assertGreater(receipt.released_chars, 0)
        self.runtime = MemoryRuntime(self.config)
        page = self.runtime.read_stream(self.access, self.event.source, limit=4096)
        self.assertEqual(page.text, first.text[:receipt.released_chars])
        self.assertFalse(page.complete)
        self.assertEqual(self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id), receipt)
        final = replace(self.event, completion="complete", text="。第二块结束")
        last = self.runtime.append_stream(self.access, final, index=1, carry=first.text[receipt.released_chars:], input_id=self.input.message_id)
        self.assertEqual(last.state, "complete")
        text = ""; cursor = 0
        while True:
            page = self.runtime.read_stream(self.access, self.event.source, cursor=cursor, limit=5)
            text += page.text
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        self.assertEqual(text, first.text + final.text)
        self.assertTrue(page.complete)
        self.assertIsNotNone(last.final_message_id)
        stored = self.runtime.read(self.access, last.final_message_id or "missing")
        assert stored is not None
        self.assertEqual(stored.event.text, text)
        self.assertEqual(stored.event.completion, "complete")
        with self.assertRaisesRegex(MemoryFailure, "stream_chunk_conflict"):
            self.runtime.append_stream(self.access, replace(first, text="不同文本"), index=0, carry="", input_id=self.input.message_id)
        with self.assertRaisesRegex(MemoryFailure, "stream_closed"):
            self.runtime.append_stream(self.access, final, index=2, carry="", input_id=self.input.message_id)

    def test_exhausted_data_chunk_limit_still_allows_an_empty_terminal_transition(self) -> None:
        with patch("evernook.durable_stream.MAX_CHUNKS", 2):
            for index in range(2):
                self.runtime.append_stream(self.access, replace(self.event, text="合成片段。"), index=index, carry="", input_id=self.input.message_id)
            with self.assertRaisesRegex(MemoryFailure, "invalid_stream_chunk"):
                self.runtime.append_stream(self.access, replace(self.event, text="不能再写。"), index=2, carry="", input_id=self.input.message_id)
            receipt = self.runtime.append_stream(self.access, replace(self.event, completion="canceled"), index=2, carry="", input_id=self.input.message_id)
            self.assertEqual(receipt.state, "canceled")
            self.assertEqual(self.runtime.read_stream(self.access, self.event.source).text, "合成片段。合成片段。")

    def test_split_sensitive_signal_is_never_released_or_persisted_as_original(self) -> None:
        text = "普通前缀。password=synthetic-private-stream-value"
        carry = ""; released = ""
        for index, character in enumerate(text):
            receipt = self.runtime.append_stream(self.access, replace(self.event, text=character),
                index=index, carry=carry, input_id=self.input.message_id)
            combined = carry + character
            released += combined[:receipt.released_chars]
            carry = combined[receipt.released_chars:]
            if receipt.state == "protected":
                break
        self.assertEqual(released, "普通前缀。")
        page = MemoryRuntime(self.config).read_stream(self.access, self.event.source)
        self.assertEqual(page.text, released)
        self.assertEqual(page.state, "protected")
        self.assertFalse(page.complete)
        for path in self.config.parent.glob("memory.sqlite3*"):
            self.assertNotIn(b"password", path.read_bytes())
            self.assertNotIn(b"synthetic-private-stream-value", path.read_bytes())

    def test_missing_or_wrong_tail_cannot_resume_but_cancel_preserves_committed_prefix(self) -> None:
        receipt = self.runtime.append_stream(self.access, replace(self.event, text="普通。pass"), index=0, carry="", input_id=self.input.message_id)
        self.assertEqual(receipt.pending_chars, 4)
        with self.assertRaisesRegex(MemoryFailure, "stream_carry_mismatch"):
            self.runtime.append_stream(self.access, replace(self.event, text="word=value"), index=1, carry="", input_id=self.input.message_id)
        final = self.runtime.append_stream(self.access, replace(self.event, completion="canceled"), index=1, carry="", input_id=self.input.message_id)
        self.assertEqual(final.state, "canceled")
        self.assertEqual(final.pending_chars, 4)
        page = self.runtime.read_stream(self.access, self.event.source)
        self.assertEqual(page.text, "普通。")
        self.assertFalse(page.complete)

    def test_existing_signal_families_remain_protected_at_every_character_boundary(self) -> None:
        signals = ("password =synthetic", "api key :synthetic", "access_token='synthetic'", "private-key是synthetic",
            "密码 为synthetic", "Bearer synthetic", "sk-abcdefghijklmnop", "ghp_abcdefghijklmnop",
            "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----", "https://synthetic:secret@example.test")
        for number, signal in enumerate(signals):
            event = replace(self.event, source=replace(self.event.source, event=f"signal-{number}"))
            carry = ""; released = ""
            for index, character in enumerate("普通。" + signal):
                receipt = self.runtime.append_stream(self.access, replace(event, text=character),
                    index=index, carry=carry, input_id=self.input.message_id)
                combined = carry + character
                released += combined[:receipt.released_chars]
                carry = combined[receipt.released_chars:]
                if receipt.state == "protected":
                    break
            self.assertEqual(receipt.state, "protected", signal)
            self.assertEqual(released, "普通。", signal)

    def test_real_process_exit_before_commit_rolls_back_and_after_ack_preserves_exact_text(self) -> None:
        script = """from contextlib import contextmanager
from pathlib import Path
import json,os,sys
from evernook import Access,Event,Source,MemoryRuntime
r=MemoryRuntime(Path(sys.argv[1])); v=json.loads((r.root/'client.json').read_text()); a=Access(v['client_id'],v['token'])
if sys.argv[3]=='before':
 original=r._connection
 @contextmanager
 def connection():
  with original() as db:
   db.set_trace_callback(lambda sql: os._exit(77) if sql.startswith('UPDATE text_streams_v1 SET') else None)
   yield db
 r._connection=connection
e=Event(Source('test','s','b','stream'),'assistant',1,None,'partial','合成已提交片段。')
v=r.append_stream(a,e,index=0,carry='',input_id=sys.argv[2])
print(json.dumps({'released_chars':v.released_chars}),flush=True)
os._exit(78)
"""
        before = subprocess.run([sys.executable, "-c", script, str(self.config), self.input.message_id, "before"], capture_output=True, timeout=30)
        self.assertEqual(before.returncode, 77, before.stderr)
        self.assertEqual(before.stdout, b"")
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            MemoryRuntime(self.config).read_stream(self.access, self.event.source)
        after = subprocess.run([sys.executable, "-c", script, str(self.config), self.input.message_id, "after"], capture_output=True, timeout=30)
        self.assertEqual(after.returncode, 78, after.stderr)
        self.assertEqual(json.loads(after.stdout)["released_chars"], len("合成已提交片段。"))
        page = MemoryRuntime(self.config).read_stream(self.access, self.event.source)
        self.assertEqual(page.text, "合成已提交片段。")
        self.assertEqual(page.state, "open")

    def test_concurrent_duplicate_chunks_and_storage_failure_keep_one_ordered_result(self) -> None:
        first = replace(self.event, text="合成首块。")
        def append() -> object:
            return self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: append(), range(2)))
        self.assertEqual(receipts[0], receipts[1])
        with self.runtime._connection() as db:
            db.execute("CREATE TRIGGER synthetic_stream_failure BEFORE INSERT ON text_stream_chunks_v1 WHEN NEW.chunk_index=1 BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        second = replace(self.event, text="第二块。")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.runtime.append_stream(self.access, second, index=1, carry="", input_id=self.input.message_id)
        page = self.runtime.read_stream(self.access, self.event.source)
        self.assertEqual(page.text, first.text)
        self.assertEqual(page.next_index, 1)
        with self.runtime._connection() as db:
            db.execute("DROP TRIGGER synthetic_stream_failure")
        receipt = self.runtime.append_stream(self.access, second, index=1, carry="", input_id=self.input.message_id)
        self.assertEqual(receipt.committed_chars, len(first.text + second.text))

    def test_confirmed_purge_removes_stream_chunks_and_source_tombstone_prevents_recreation(self) -> None:
        first = replace(self.event, text="合成普通分块。")
        receipt = self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id)
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, "purge-stream", (receipt.stream_id,))
        confirmation = self.runtime.submit(self.access, Event(Source("test", "s", "b", "confirm"), "user", 2, None, "complete", "合成确认删除"))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            self.runtime.read_stream(self.access, self.event.source)
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id)
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.submit(self.access, replace(first, completion="complete"))
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.submit_capture(self.access, Capture(replace(first, completion="complete"), "message"))

    def test_suppressing_completed_answer_blocks_stream_replay_and_old_ack(self) -> None:
        final = replace(self.event, completion="complete", text="合成完成。")
        receipt = self.runtime.append_stream(self.access, final, index=0, carry="", input_id=self.input.message_id)
        confirmation = self.runtime.submit(self.access, Event(Source("test", "s", "b", "suppress"), "user", 2, None, "complete", "合成不再召回"))
        self.runtime.change(self.access, Change("suppress-answer", receipt.final_message_id or "missing", 1, "suppress", confirmation.message_id, "合成决定"))
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            self.runtime.read_stream(self.access, self.event.source)
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            self.runtime.append_stream(self.access, final, index=0, carry="", input_id=self.input.message_id)

    def test_suppressed_input_blocks_both_partial_replay_and_continuation(self) -> None:
        first = replace(self.event, text="合成片段。")
        self.runtime.append_stream(self.access, first, index=0, carry="", input_id=self.input.message_id)
        instruction = self.runtime.submit(self.access, Event(Source("test", "s", "b", "hide-input"), "user", 2, None, "complete", "合成不再召回输入来源"))
        self.runtime.change(self.access, Change("hide-stream-input", self.input.message_id, 1, "suppress", instruction.message_id, "合成决定"))
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            self.runtime.read_stream(self.access, self.event.source)
        with self.assertRaisesRegex(MemoryFailure, "stream_unavailable"):
            self.runtime.append_stream(self.access, first, index=1, carry="", input_id=self.input.message_id)

    def test_ordinary_and_stream_routes_cannot_claim_or_revive_each_others_source(self) -> None:
        event = replace(self.event, text="合成普通已抑制内容。", completion="complete")
        saved = self.runtime.submit(self.access, event)
        instruction = self.runtime.submit(self.access, Event(Source("test", "s", "b", "suppress-original"), "user", 2, None, "complete", "合成抑制"))
        self.runtime.change(self.access, Change("hide-original-answer", saved.message_id, 1, "suppress", instruction.message_id, "合成"))
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            self.runtime.append_stream(self.access, replace(event, completion="partial"), index=0, carry="", input_id=self.input.message_id)
        workflow = PurgeWorkflow(self.runtime)
        workflow.preview(self.access, "remove-original-answer", (saved.message_id,))
        confirmation = self.runtime.submit(self.access, Event(Source("test", "s", "b", "purge-original"), "user", 3, None, "complete", "合成确认删除"))
        workflow.purge(self.access, PurgeDecision("remove-original-answer", confirmation.message_id))
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.append_stream(self.access, replace(event, completion="partial"), index=0, carry="", input_id=self.input.message_id)
        fresh = replace(event, source=replace(event.source, event="reserved-answer"), completion="partial")
        self.runtime.append_stream(self.access, fresh, index=0, carry="", input_id=self.input.message_id)
        with self.assertRaisesRegex(MemoryFailure, "source_reserved_for_stream"):
            self.runtime.submit(self.access, replace(fresh, completion="complete", text="合成另一个答案。"))
        with self.assertRaisesRegex(MemoryFailure, "source_reserved_for_stream"):
            self.runtime.submit_capture(self.access, Capture(replace(fresh, completion="complete"), "message"))
        last = self.runtime.append_stream(self.access, replace(fresh, completion="complete", text=""), index=1, carry="", input_id=self.input.message_id)
        self.assertEqual(last.state, "complete")
        self.assertEqual(self.runtime.submit(self.access, replace(fresh, completion="complete")).message_id, last.final_message_id)


if __name__ == "__main__":
    unittest.main()
