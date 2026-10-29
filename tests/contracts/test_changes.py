from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier, Event as ThreadEvent
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Change, DerivedContext, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize


class ChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-changes-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test",
                                               allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.old_event = Event(Source("test", "session", "main", "old"), "user", 1, None, "complete", "合成金额：37.50元。")
        self.old = self.runtime.submit(self.access, self.old_event)
        self.new = self.runtime.submit(self.access, Event(Source("test", "session", "main", "correction"),
            "user", 2, None, "complete", "更正合成金额：38.50元。"))

    def test_correction_immediately_blocks_old_reads_but_preserves_archive_and_new_evidence(self) -> None:
        change = Change("synthetic-change-1", self.old.message_id, 1, "correct", self.new.message_id, "synthetic correction")
        state = self.runtime.change(self.access, change)
        self.assertEqual((state.version, state.status, state.replacement_id), (2, "corrected", self.new.message_id))
        self.assertIsNone(self.runtime.read(self.access, self.old.message_id))
        self.assertIsNone(self.runtime.read_source(self.access, self.old_event.source))
        self.assertEqual([item.receipt.message_id for item in self.runtime.list_messages(self.access).messages], [self.new.message_id])
        archive = self.runtime.review_archive(self.access, self.old.message_id)
        self.assertEqual(archive.message.event, self.old_event)
        self.assertEqual(archive.state, state)
        self.assertEqual(archive.changes[0].reason, "synthetic correction")
        self.assertTrue(archive.archive_only)
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.change(self.access, change), state)
        self.assertEqual(reopened.state(self.access, self.old.message_id), state)
        self.assertIsNone(reopened.read(self.access, self.old.message_id))
        self.assertIsNotNone(reopened.read(self.access, self.new.message_id))

    def test_suppressed_important_memory_cannot_return_or_be_marked_again_after_restart(self) -> None:
        mark = ImportantMark(self.old.message_id, "original", 1, "synthetic-shared", None, None, self.old_event.text)
        item = self.runtime.mark_important(self.access, mark)
        request = Change("synthetic-suppress", self.old.message_id, 1, "suppress", self.new.message_id, "synthetic suppression")
        state = self.runtime.change(self.access, request)
        self.assertEqual(state.status, "suppressed")
        for runtime in [self.runtime, MemoryRuntime(self.config)]:
            with self.assertRaisesRegex(MemoryFailure, "not_recallable"):
                runtime.read_important(self.access, item.important_id)
            with self.assertRaisesRegex(MemoryFailure, "not_recallable"):
                runtime.mark_important(self.access, mark)
            self.assertEqual(runtime.submit(self.access, self.old_event), self.old)
            self.assertIsNone(runtime.read(self.access, self.old.message_id))
            self.assertEqual(runtime.status(self.access).important_items, 1)
        self.assertEqual(self.runtime.review_archive(self.access, self.old.message_id).message.event, self.old_event)

    def test_concurrent_versions_conflict_and_old_retries_cannot_overwrite_new_confirmation(self) -> None:
        requests = [Change("synthetic-a", self.old.message_id, 1, "suppress", self.new.message_id, "synthetic-a"),
                    Change("synthetic-b", self.old.message_id, 1, "revoke", self.new.message_id, "synthetic-b")]
        barrier = Barrier(2)

        def submit(request: Change) -> str:
            barrier.wait(timeout=10)
            try:
                self.runtime.change(self.access, request)
                return request.request_id
            except MemoryFailure as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, requests))
        self.assertEqual(results.count("version_conflict"), 1)
        winner = next(request for request in requests if request.request_id in results)
        loser = next(request for request in requests if request.request_id not in results)
        old_receipt = self.runtime.change(self.access, winner)
        newer = self.runtime.change(self.access, replace(loser, expected_version=2))
        self.assertEqual(newer.version, 3)
        self.assertEqual(self.runtime.change(self.access, winner), old_receipt)
        self.assertEqual(self.runtime.state(self.access, self.old.message_id), newer)
        with self.assertRaisesRegex(MemoryFailure, "change_conflict"):
            self.runtime.change(self.access, replace(winner, reason="different"))
        self.assertEqual(len(self.runtime.review_archive(self.access, self.old.message_id).changes), 2)

    def test_change_refuses_unknown_target_missing_ambiguous_or_protected_evidence(self) -> None:
        request = Change("synthetic-invalid", self.old.message_id, 1, "correct", self.new.message_id, "synthetic reason")
        for changed, code in [
            (replace(request, target_id="unknown"), "message_not_found"),
            (replace(request, evidence_id="unknown"), "insufficient_evidence"),
            (replace(request, evidence_id=self.old.message_id), "insufficient_evidence"),
            (replace(request, expected_version=0), "invalid_change"),
            (replace(request, action="restore"), "invalid_change"),
            (replace(request, reason="password=synthetic-only"), "sensitive_metadata"),
        ]:
            with self.assertRaisesRegex(MemoryFailure, code):
                self.runtime.change(self.access, changed)
        for number, (role, completion, sensitive) in enumerate([("assistant", "complete", False), ("user", "partial", False), ("user", "complete", True)]):
            evidence = self.runtime.submit(self.access, Event(Source("test", "session", "main", str(number)),
                role, number, None, completion, "synthetic unconfirmed evidence", sensitive=sensitive))
            with self.assertRaisesRegex(MemoryFailure, "insufficient_evidence"):
                self.runtime.change(self.access, replace(request, evidence_id=evidence.message_id))
        blank = self.runtime.submit(self.access, Event(Source("test", "session", "main", "blank"),
            "user", 4, None, "complete", " \r\n\t"))
        with self.assertRaisesRegex(MemoryFailure, "insufficient_evidence"):
            self.runtime.change(self.access, replace(request, evidence_id=blank.message_id))
        self.assertEqual(self.runtime.state(self.access, self.old.message_id).version, 1)
        self.assertEqual(self.runtime.review_archive(self.access, self.old.message_id).changes, ())

    def test_archive_requires_server_grant_and_legacy_credentials_do_not_gain_it(self) -> None:
        config, access = initialize(self.config.parent.parent / "ordinary", client_id="synthetic", entry="test")
        ordinary = MemoryRuntime(config)
        receipt = ordinary.submit(access, self.old_event)
        with self.assertRaisesRegex(MemoryFailure, "archive_forbidden"):
            ordinary.review_archive(access, receipt.message_id)
        client_path = config.parent / "client.json"
        credentials = json.loads(client_path.read_text(encoding="utf-8"))
        credentials.pop("archive_review")
        credentials["schema_version"] = 1
        client_path.write_text(json.dumps(credentials), encoding="utf-8")
        legacy = MemoryRuntime(config)
        self.assertFalse(legacy.handshake(access)["archive_review"])
        self.assertIsNotNone(legacy.read(access, receipt.message_id))
        with self.assertRaisesRegex(MemoryFailure, "archive_forbidden"):
            legacy.review_archive(access, receipt.message_id)

    def test_old_background_results_cannot_publish_or_reappear_after_suppression(self) -> None:
        result = DerivedContext("synthetic-result", self.old.message_id, 1, "合成旧结果，金额37.50。")
        receipt = self.runtime.publish_context(self.access, result)
        self.assertEqual(self.runtime.publish_context(self.access, result), receipt)
        self.assertEqual(self.runtime.read_context(self.access, result.result_id), result)
        started, resume = ThreadEvent(), ThreadEvent()

        def old_worker() -> str:
            version = self.runtime.state(self.access, self.old.message_id).version
            started.set()
            if not resume.wait(timeout=10):
                raise TimeoutError("synthetic worker not resumed")
            try:
                self.runtime.publish_context(self.access, replace(result, result_id="in-flight-result", source_version=version))
                return "unexpectedly_published"
            except MemoryFailure as error:
                return error.code

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_worker)
            self.assertTrue(started.wait(timeout=10))
            try:
                self.runtime.change(self.access, Change("synthetic-suppress", self.old.message_id, 1, "suppress",
                                                        self.new.message_id, "synthetic suppression"))
            finally:
                resume.set()
            self.assertEqual(future.result(timeout=10), "stale_source")
        for runtime in [self.runtime, MemoryRuntime(self.config)]:
            self.assertIsNone(runtime.read_context(self.access, result.result_id))
            with self.assertRaisesRegex(MemoryFailure, "stale_source"):
                runtime.publish_context(self.access, result)
            with self.assertRaisesRegex(MemoryFailure, "stale_source"):
                runtime.publish_context(self.access, replace(result, result_id="synthetic-late-result"))
        self.assertEqual(self.runtime.review_archive(self.access, self.old.message_id).message.event, self.old_event)

    def test_publication_gate_rejects_sensitive_results_versions_and_conflicting_ids(self) -> None:
        context = DerivedContext("synthetic-result", self.old.message_id, 1, "synthetic derived context")
        self.runtime.publish_context(self.access, context)
        with self.assertRaisesRegex(MemoryFailure, "context_conflict"):
            self.runtime.publish_context(self.access, replace(context, text="different"))
        for request, code in [
            (replace(context, source_version=2), "stale_source"),
            (replace(context, protected=True), "protected_context"),
            (replace(context, text="password=synthetic-only"), "protected_context"),
            (replace(context, text="bad\ud800"), "invalid_encoding"),
        ]:
            with self.assertRaisesRegex(MemoryFailure, code):
                self.runtime.publish_context(self.access, request)
        protected = self.runtime.submit(self.access, Event(Source("test", "session", "main", "protected"),
            "user", 3, None, "complete", "synthetic protected body", sensitive=True))
        with self.assertRaisesRegex(MemoryFailure, "protected_context"):
            self.runtime.publish_context(self.access, replace(context, result_id="projection-result", source_id=protected.message_id))
        self.assertEqual(self.runtime.read_context(self.access, context.result_id), context)
        self.assertIsNone(self.runtime.read_context(self.access, "projection-result"))

    def test_sqlite_full_does_not_commit_half_change_and_failed_request_can_retry(self) -> None:
        real_connect = sqlite3.connect

        def quota_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = real_connect(*args, **kwargs)
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            connection.execute(f"PRAGMA max_page_count={pages}")
            return connection

        previous = self.runtime.state(self.access, self.old.message_id)
        failed: Change | None = None
        with patch("sqlite3.connect", quota_connect):
            for number in range(50):
                request = Change(f"synthetic-full-{number}", self.old.message_id, previous.version,
                                 "suppress", self.new.message_id, "synthetic reason " + "x" * 200)
                try:
                    previous = self.runtime.change(self.access, request)
                except MemoryFailure as error:
                    self.assertEqual(error.code, "storage_full")
                    failed = request
                    break
        assert failed is not None, "synthetic SQLite page quota was not reached"
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.state(self.access, self.old.message_id), previous)
        archive = reopened.review_archive(self.access, self.old.message_id)
        self.assertEqual(len(archive.changes), previous.version - 1)
        self.assertEqual(archive.message.event, self.old_event)
        retried = reopened.change(self.access, failed)
        self.assertEqual(retried.version, previous.version + 1)
        self.assertEqual(reopened.change(self.access, failed), retried)


if __name__ == "__main__":
    unittest.main()
