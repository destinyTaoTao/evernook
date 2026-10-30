from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from evernook import Change, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-recovery-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "crash-session", "main", "1"), "assistant", 1,
                           None, "partial", "合成中断片段\r\n🙂e\u0301")

    def test_busy_and_real_sqlite_page_quota_return_failure_without_partial_message_or_job(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["busy_timeout_ms"] = 0
        self.config.write_text(json.dumps(config), encoding="utf-8")
        runtime = MemoryRuntime(self.config)
        lock = sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)
        try:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(MemoryFailure, "storage_busy"):
                runtime.submit(self.access, self.event)
        finally:
            lock.close()
        self.assertEqual(runtime.status(self.access).messages, 0)
        real_connect = sqlite3.connect

        def quota_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={pages}")
            return connection

        large = Event(self.event.source, "assistant", 1, None, "partial", "合成" * 20000)
        with patch("sqlite3.connect", quota_connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_full"):
                runtime.submit(self.access, large)
        reopened = MemoryRuntime(self.config)
        self.assertIsNone(reopened.read_source(self.access, self.event.source))
        self.assertEqual(reopened.status(self.access).pending_jobs, 0)
        self.assertTrue(reopened.submit(self.access, self.event).original_saved)

    def test_process_exit_before_commit_rolls_back_and_after_receipt_recovers_exact_original(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        before = subprocess.run([sys.executable, str(worker), str(self.config), "before_commit"],
                                capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(before.returncode, 86, before.stderr)
        reopened = MemoryRuntime(self.config)
        self.assertIsNone(reopened.read_source(self.access, self.event.source))
        self.assertEqual(reopened.status(self.access).pending_jobs, 0)
        after = subprocess.run([sys.executable, str(worker), str(self.config), "after_receipt"],
                               capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(after.returncode, 87, after.stderr)
        message_id = json.loads(after.stdout)["message_id"]
        recovered = MemoryRuntime(self.config).read(self.access, message_id)
        assert recovered is not None
        self.assertEqual(recovered.event, self.event)
        self.assertEqual(reopened.submit(self.access, self.event), recovered.receipt)
        self.assertEqual(reopened.status(self.access).messages, 1)
        self.assertEqual(reopened.status(self.access).pending_jobs, 1)

    def test_important_mark_process_exit_never_leaves_half_reference_or_loses_acknowledged_mark(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        for mode, code in [("important_before_commit", 86), ("important_after_receipt", 87)]:
            result = subprocess.run([sys.executable, str(worker), str(self.config), mode],
                                    capture_output=True, text=True, timeout=20, env=environment)
            self.assertEqual(result.returncode, code, result.stderr)
            runtime = MemoryRuntime(self.config)
            original = runtime.read_source(self.access, self.event.source)
            assert original is not None
            if mode == "important_before_commit":
                self.assertEqual(runtime.status(self.access).important_items, 0)
            else:
                important_id = json.loads(result.stdout)["important_id"]
                page = runtime.read_important(self.access, important_id)
                self.assertEqual(page.text, self.event.text)
                mark = ImportantMark(original.receipt.message_id, "original", 1, "synthetic-shared", None, None, self.event.text)
                self.assertEqual(runtime.mark_important(self.access, mark), page.item)
                self.assertEqual(runtime.status(self.access).important_items, 1)
            self.assertEqual(runtime.status(self.access).pending_jobs, 1)

    def test_change_process_exit_rolls_back_or_preserves_visibility_and_idempotent_receipt_together(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        for mode, code in [("change_before_commit", 86), ("change_after_receipt", 87)]:
            result = subprocess.run([sys.executable, str(worker), str(self.config), mode],
                                    capture_output=True, text=True, timeout=20, env=environment)
            self.assertEqual(result.returncode, code, result.stderr)
            runtime = MemoryRuntime(self.config)
            receipt = runtime.submit(self.access, self.event)
            state = runtime.state(self.access, receipt.message_id)
            if mode == "change_before_commit":
                self.assertEqual((state.version, state.status), (1, "active"))
                self.assertIsNotNone(runtime.read(self.access, receipt.message_id))
            else:
                self.assertEqual((state.version, state.status), (2, "suppressed"))
                self.assertIsNone(runtime.read(self.access, receipt.message_id))
                evidence = runtime.read_source(self.access, Source("test", "crash-session", "main", "evidence"))
                assert evidence is not None
                request = Change("synthetic-change", receipt.message_id, 1, "suppress", evidence.receipt.message_id, "synthetic suppression")
                self.assertEqual(runtime.change(self.access, request), state)

    def test_capture_process_exit_rolls_back_or_recovers_manifest_message_and_attachment_together(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        before = subprocess.run([sys.executable, str(worker), str(self.config), "capture_before_commit"],
                                capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(before.returncode, 86, before.stderr)
        reopened = MemoryRuntime(self.config)
        self.assertIsNone(reopened.read_source(self.access, self.event.source))
        self.assertEqual(reopened.status(self.access).messages, 1)
        self.assertEqual(reopened.status(self.access).pending_jobs, 1)
        after = subprocess.run([sys.executable, str(worker), str(self.config), "capture_after_receipt"],
                               capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(after.returncode, 87, after.stderr)
        message_id = json.loads(after.stdout)["message_id"]
        recovered = MemoryRuntime(self.config)
        captured = recovered.read_capture(self.access, message_id)
        assert captured is not None
        self.assertEqual(captured.gaps, ("incomplete_output",))
        self.assertFalse(captured.complete)
        self.assertEqual(recovered.read_attachment(self.access, message_id, "a1"), b"synthetic attachment")
        message = recovered.read(self.access, message_id)
        assert message is not None
        self.assertEqual(message.event, self.event)
        self.assertEqual(recovered.status(self.access).messages, 2)
        self.assertEqual(recovered.status(self.access).pending_jobs, 2)

    def test_oversize_capture_worker_reports_gap_without_pathological_detection_scan(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        result = subprocess.run([sys.executable, str(worker), str(self.config), "bounded_oversize"],
                                capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertIn("size_limit", receipt["gaps"])
        self.assertFalse(receipt["original_saved"])

    def test_index_commit_interruption_preserves_originals_and_recovers_without_half_coverage(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        before = subprocess.run([sys.executable, str(worker), str(self.config), "lexical_before_commit"],
                                capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(before.returncode, 86, before.stderr)
        reopened = MemoryRuntime(self.config)
        pending = reopened.search(self.access, "片段")
        self.assertEqual((pending.index.state, pending.index.indexed_messages, pending.index.pending_messages), ("behind", 0, 1))
        self.assertEqual(len(pending.hits), 1)
        original = reopened.read(self.access, pending.hits[0].message_id)
        assert original is not None
        self.assertEqual(original.event, self.event)
        after = subprocess.run([sys.executable, str(worker), str(self.config), "lexical_after_receipt"],
                               capture_output=True, text=True, timeout=20, env=environment)
        self.assertEqual(after.returncode, 87, after.stderr)
        self.assertEqual(json.loads(after.stdout)["state"], "ready")
        recovered = MemoryRuntime(self.config)
        indexed = recovered.search(self.access, "片段")
        self.assertTrue(indexed.used_index)
        self.assertEqual(indexed.hits, pending.hits)
        self.assertEqual(recovered.refresh_lexical(self.access).pending_messages, 0)
        self.assertEqual(recovered.status(self.access).messages, 1)
        self.assertEqual(recovered.status(self.access).pending_jobs, 1)


if __name__ == "__main__":
    unittest.main()
