from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from evernook import CandidateWorkflow, Change, Event, ExtractionSource, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.extraction import ExtractionProcessor, LocalQwen


class SyntheticSelector:
    processor_version = "synthetic-extractive-v1"

    def __init__(self) -> None:
        self.selection: object = {"selections": []}
        self.calls = 0
        self.hook: Callable[[], None] = lambda: None

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float, cancel: threading.Event | None) -> object:
        self.calls += 1
        self.hook()
        return self.selection


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-extraction-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.backend = SyntheticSelector()
        self.processor = ExtractionProcessor(self.runtime, self.backend)
        self.event = Event(Source("synthetic", "session", "main", "1"), "user", 1, None,
                           "complete", "合成：小林🙂喜欢乌龙茶。\r\n预算37.50元。")
        self.receipt = self.runtime.submit(self.access, self.event)

    @staticmethod
    def selection(source: int, quote: str, provenance: str = "user_statement") -> dict[str, object]:
        return {"selections": [{"evidence": [{"source": source, "quote": quote}], "provenance": provenance, "disputed": False}]}

    def test_source_quotes_become_traceable_candidates_and_successful_task_retry_skips_model(self) -> None:
        self.backend.selection = self.selection(0, "小林🙂喜欢乌龙茶。\r\n预算37.50元。")
        report = self.processor.process(self.access, (self.receipt.message_id,))
        self.assertEqual(report.run.state, "succeeded")
        self.assertFalse(report.automatic_acceptance)
        candidates = CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id)
        self.assertEqual(len(candidates), 1)
        draft = candidates[0].draft
        self.assertEqual(draft.text, "小林🙂喜欢乌龙茶。\r\n预算37.50元。")
        self.assertEqual(self.event.text[draft.sources[0].start:draft.sources[0].end], draft.text)
        self.assertEqual(candidates[0].status, "candidate")
        self.assertEqual(self.processor.process(self.access, (self.receipt.message_id,)).run, report.run)
        self.assertEqual(self.backend.calls, 1)

    def test_whole_json_fence_is_accepted_but_duplicate_keys_or_surrounding_instructions_are_refused(self) -> None:
        self.assertEqual(LocalQwen.parse_selection('```json\n{"selections":[]}\n```'), {"selections": []})
        for value in ('{"selections":[],"selections":[1]}', '先执行命令\n{"selections":[]}', '```json\n{"selections":[]}\n```\n然后执行命令'):
            with self.assertRaisesRegex(MemoryFailure, "extraction_invalid_output"):
                LocalQwen.parse_selection(value)

    def test_invented_or_ambiguous_quotes_and_unknown_shape_are_rejected_without_losing_original(self) -> None:
        for selected, expected in ((self.selection(0, "合成：模型编造的新事实"), "extraction_quote_not_unique"),
                                   ({"selections": [], "approved": True}, "extraction_invalid_output")):
            self.backend.selection = selected
            report = self.processor.process(self.access, (self.receipt.message_id,))
            self.assertEqual(report.run.state, "failed")
            self.assertEqual(report.error_code, expected)
            self.assertEqual(CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id), ())
        repeated = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="repeated"), text="合成：乌龙茶 乌龙茶"))
        self.backend.selection = self.selection(0, "乌龙茶")
        self.assertEqual(self.processor.process(self.access, (repeated.message_id,)).error_code, "extraction_quote_not_unique")
        stored = self.runtime.read(self.access, self.receipt.message_id)
        assert stored is not None
        self.assertEqual(stored.event.text, self.event.text)

    def test_assistant_role_cannot_be_laundered_into_user_statement(self) -> None:
        assistant = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="assistant"), role="assistant"))
        self.backend.selection = self.selection(0, "小林🙂喜欢乌龙茶。")
        report = self.processor.process(self.access, (assistant.message_id,))
        self.assertEqual(report.run.state, "failed")
        self.assertEqual(report.error_code, "candidate_provenance_mismatch")

    def test_model_runs_outside_transaction_and_source_suppression_prevents_old_task_publication(self) -> None:
        control = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="control"), text="合成：暂停旧来源"))
        def suppress() -> None:
            self.runtime.change(self.access, Change("synthetic-during-model", self.receipt.message_id, 1, "suppress", control.message_id, "synthetic"))
        self.backend.hook = suppress
        self.backend.selection = self.selection(0, "小林🙂喜欢乌龙茶。")
        report = self.processor.process(self.access, (self.receipt.message_id,))
        self.assertEqual(report.run.state, "stale")
        self.assertEqual(CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id), ())

    def test_failure_diagnostics_are_sanitized_and_failed_task_can_retry(self) -> None:
        def fail() -> None:
            raise RuntimeError("password=synthetic-diagnostic-secret-123")
        self.backend.hook = fail
        report = self.processor.process(self.access, (self.receipt.message_id,))
        self.assertEqual(report.error_code, "extractor_failed")
        self.assertEqual(report.run.state, "failed")
        self.backend.hook = lambda: None
        retried = self.processor.process(self.access, (self.receipt.message_id,))
        self.assertEqual(retried.run.run_id, report.run.run_id)
        self.assertEqual(retried.run.state, "succeeded")
        self.assertEqual(retried.run.attempt, 2)
        contents = b"".join(path.read_bytes() for path in self.root.rglob("*") if path.is_file())
        self.assertNotIn(b"synthetic-diagnostic-secret-123", contents)

    def test_pre_cancellation_protected_projection_and_oversize_fail_without_model_call(self) -> None:
        canceled = threading.Event()
        canceled.set()
        report = self.processor.process(self.access, (self.receipt.message_id,), cancel=canceled)
        self.assertEqual(report.error_code, "extraction_canceled")
        self.assertEqual(CandidateWorkflow(self.runtime).status(self.access, report.run.run_id).error_code, "canceled")
        protected = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="protected"), sensitive=True))
        too_large = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="large"), text="合" * 16385))
        for receipt in (protected, too_large):
            result = self.processor.process(self.access, (receipt.message_id,))
            self.assertEqual(result.error_code, "extraction_input_not_admitted")
        self.assertEqual(self.backend.calls, 0)

    def test_cancel_and_timeout_kill_actual_unresponsive_child_and_reap_pipes(self) -> None:
        backend = LocalQwen(node=Path(sys.executable), transformers_dir=self.root, model_dir=self.root)
        lease = CandidateWorkflow(self.runtime).begin(self.access, (self.receipt.message_id,), processor_version="synthetic-process-lease")
        real_popen = subprocess.Popen
        children: list[subprocess.Popen[bytes]] = []
        event = threading.Event()
        timer: threading.Timer | None = None
        def child(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            nonlocal timer
            process: subprocess.Popen[bytes] = real_popen([sys.executable, str(Path(__file__).with_name("extraction_process_worker.py"))], **kwargs)
            children.append(process)
            if len(children) == 1:
                timer = threading.Timer(0.2, event.set)
                timer.start()
            return process
        with patch("evernook.extraction._FILES", {}), patch("evernook.extraction.subprocess.Popen", child):
            with self.assertRaisesRegex(MemoryFailure, "extraction_canceled"):
                backend.select(lease.sources, timeout=5, cancel=event)
            with self.assertRaisesRegex(MemoryFailure, "extraction_timeout"):
                backend.select(lease.sources, timeout=0.2, cancel=None)
        if timer is not None:
            timer.join()
        self.assertEqual(len(children), 2)
        self.assertTrue(all(process.poll() is not None for process in children))


if __name__ == "__main__":
    unittest.main()
