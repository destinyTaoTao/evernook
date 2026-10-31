from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import time
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-candidates-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "s", "main", "1"), "user", 1, None, "complete", "合成小林喜欢乌龙茶。")
        self.receipt = self.runtime.submit(self.access, self.event)

    def test_candidate_batch_is_durable_source_bound_and_idempotent_without_promoting_facts(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        self.assertIsNone(lease.status.completed_watermark)
        self.assertEqual(lease.sources[0].message.event, self.event)
        self.assertEqual(lease.sources[0].state_version, 1)
        assert lease.token is not None
        draft = CandidateDraft("合成小林喜欢乌龙茶", "user_statement",
            (EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, "合成小林喜欢乌龙茶。"),))
        complete = workflow.complete(self.access, lease.status.run_id, lease.token, (draft, draft))
        self.assertEqual(complete.state, "succeeded")
        self.assertEqual(complete.candidate_count, 1)
        self.assertEqual(complete.completed_watermark, complete.source_watermark)
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, (draft,)), complete)
        reopened = CandidateWorkflow(MemoryRuntime(self.config))
        candidates = reopened.candidates(self.access, complete.run_id)
        self.assertEqual(candidates[0].draft, draft)
        self.assertEqual(candidates[0].status, "candidate")
        repeated = reopened.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        self.assertEqual(repeated.status.run_id, complete.run_id)
        self.assertIsNone(repeated.token)
        self.assertEqual(self.runtime.status(self.access).pending_jobs, 1)
        self.assertEqual(self.runtime.search(self.access, "合成小林喜欢乌龙茶").hits[0].message_id, self.receipt.message_id)

    def test_expired_or_failed_runs_retry_with_new_lease_and_old_tokens_cannot_publish(self) -> None:
        from evernook import CandidateWorkflow

        workflow = CandidateWorkflow(self.runtime)
        first = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1", lease_seconds=1)
        assert first.token is not None and first.status.lease_until is not None
        with self.assertRaisesRegex(MemoryFailure, "candidate_run_busy"):
            workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        # A controllable clock replaces the thin wall-clock margin: a system time step cannot flake these checks.
        with patch("evernook.candidates.time.time", return_value=first.status.lease_until + 1):
            self.assertEqual(workflow.status(self.access, first.status.run_id).state, "expired")
            with self.assertRaisesRegex(MemoryFailure, "candidate_lease_expired"):
                workflow.complete(self.access, first.status.run_id, first.token, ())
            second = CandidateWorkflow(MemoryRuntime(self.config)).begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
            self.assertEqual(second.status.run_id, first.status.run_id)
            self.assertEqual(second.status.attempt, 2)
            self.assertNotEqual(first.token, second.token)
            with self.assertRaisesRegex(MemoryFailure, "invalid_candidate_lease"):
                workflow.complete(self.access, first.status.run_id, first.token, ())
            assert second.token is not None
            failed = workflow.fail(self.access, second.status.run_id, second.token)
            self.assertEqual((failed.state, failed.error_code), ("failed", "extractor_failed"))
            third = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
            self.assertEqual(third.status.attempt, 3)
            assert third.token is not None
            self.assertEqual(workflow.complete(self.access, third.status.run_id, third.token, ()).state, "succeeded")

    def test_multisource_candidates_and_old_completed_runs_disappear_after_source_suppression(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        second_event = replace(self.event, source=replace(self.event.source, event="2"), text="合成杯子是蓝色。")
        second = self.runtime.submit(self.access, second_event)
        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (second.message_id, self.receipt.message_id), processor_version="synthetic-v1")
        draft = CandidateDraft("合成饮品与蓝色杯子", "user_statement", (
            EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text),
            EvidenceSpan(second.message_id, 1, "original", 1, 0, 8, second_event.text)))
        assert lease.token is not None
        workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        self.assertEqual(len(workflow.candidates(self.access, lease.status.run_id)), 1)
        self.assertEqual(self.runtime.search(self.access, draft.text).hits, ())
        evidence = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="3"), text="合成用户明确抑制杯子记录"))
        self.runtime.change(self.access, Change("suppress", second.message_id, 1, "suppress", evidence.message_id, "synthetic instruction"))
        self.assertEqual(workflow.candidates(self.access, lease.status.run_id), ())
        status = CandidateWorkflow(MemoryRuntime(self.config)).status(self.access, lease.status.run_id)
        self.assertEqual((status.state, status.candidate_count, status.error_code), ("stale", 0, "stale_source"))
        self.assertIsNone(status.completed_watermark)
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))

    def test_rejected_spans_roles_or_sensitive_outputs_leave_no_partial_candidates(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        emoji_event = replace(self.event, source=replace(self.event.source, event="2"), text="甲🙂e\u0301乙")
        emoji = self.runtime.submit(self.access, emoji_event)
        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (emoji.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None
        span = EvidenceSpan(emoji.message_id, 1, "original", 1, 1, 2, "🙂")
        valid = CandidateDraft("合成emoji引用", "user_statement", (span,))
        invalid = [
            (replace(valid, sources=(replace(span, end=3),)), "candidate_span_mismatch"),
            (replace(valid, sources=(replace(span, state_version=True),)), "candidate_span_mismatch"),
            (replace(valid, sources=(replace(span, representation="projection"),)), "candidate_span_mismatch"),
            (replace(valid, sources=(replace(span, message_id=self.receipt.message_id),)), "invalid_candidate_source"),
            (replace(valid, text="password=synthetic-must-never-persist"), "protected_candidate"),
            (replace(valid, provenance="assistant_generated"), "candidate_provenance_mismatch"),
        ]
        for draft, code in invalid:
            with self.assertRaisesRegex(MemoryFailure, code):
                workflow.complete(self.access, lease.status.run_id, lease.token, (valid, draft))
            self.assertEqual(workflow.candidates(self.access, lease.status.run_id), ())
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, (valid,)).candidate_count, 1)
        self.assertEqual(workflow.candidates(self.access, lease.status.run_id)[0].draft.sources[0].expected_text, "🙂")
        for path in self.config.parent.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-must-never-persist", path.read_bytes())

    def test_concurrent_claim_has_one_owner_and_cannot_overwrite_a_completed_result(self) -> None:
        from evernook import CandidateDraft, CandidateLease, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)

        def claim(_: int) -> CandidateLease | str:
            try:
                return workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
            except MemoryFailure as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(result == "candidate_run_busy" for result in results), 1)
        leases = [result for result in results if isinstance(result, CandidateLease)]
        self.assertEqual(len(leases), 1)
        lease = leases[0]
        assert lease.token is not None
        self.assertNotIn(lease.token, repr(lease))
        span = EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text)
        draft = CandidateDraft("合成候选", "user_statement", (span,))
        workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        with self.assertRaisesRegex(MemoryFailure, "candidate_result_conflict"):
            workflow.complete(self.access, lease.status.run_id, lease.token, (replace(draft, text="different"),))
        other = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v2")
        self.assertNotEqual(other.status.run_id, lease.status.run_id)
        self.assertEqual(workflow.candidates(self.access, lease.status.run_id)[0].draft, draft)

    def test_sqlite_busy_does_not_publish_candidates_or_advance_completion_watermark(self) -> None:
        from evernook import CandidateWorkflow

        configuration = json.loads(self.config.read_text(encoding="utf-8"))
        configuration["busy_timeout_ms"] = 0
        self.config.write_text(json.dumps(configuration), encoding="utf-8")
        workflow = CandidateWorkflow(MemoryRuntime(self.config))
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(MemoryFailure, "storage_busy"):
                workflow.complete(self.access, lease.status.run_id, lease.token, ())
            self.assertIsNone(workflow.status(self.access, lease.status.run_id).completed_watermark)
            self.assertEqual(workflow.candidates(self.access, lease.status.run_id), ())
            lock.rollback()
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, ()).state, "succeeded")

    def test_lease_expiring_during_completion_cannot_commit_success_or_candidates(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None and lease.status.lease_until is not None
        draft = CandidateDraft("synthetic lease boundary", "user_statement", (
            EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text),))
        with patch("evernook.candidates.time.time", side_effect=[lease.status.lease_until - 0.5, lease.status.lease_until + 1]):
            with self.assertRaisesRegex(MemoryFailure, "candidate_lease_expired"):
                workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        self.assertIsNone(workflow.status(self.access, lease.status.run_id).completed_watermark)
        self.assertEqual(workflow.candidates(self.access, lease.status.run_id), ())

    def test_process_exit_after_claim_or_around_completion_recovers_without_half_results(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        worker = Path(__file__).with_name("candidate_worker.py")
        for mode, code in [("after_claim", 85), ("before_complete_commit", 86), ("after_complete", 87)]:
            process = subprocess.run([sys.executable, str(worker), str(self.config), self.receipt.message_id, mode],
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, code, process.stderr)
            workflow = CandidateWorkflow(MemoryRuntime(self.config))
            if code != 87:
                # Advance a controlled clock past the child's stored lease instead of sleeping a
                # thin wall-clock margin that a system time step could invalidate.
                with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
                    (lease_until,) = db.execute("SELECT max(lease_until) FROM candidate_runs_v1 WHERE state='running'").fetchone()
                assert lease_until is not None
                with patch("evernook.candidates.time.time", return_value=float(lease_until) + 1):
                    retry = workflow.begin(self.access, (self.receipt.message_id,), processor_version=mode)
            else:
                retry = workflow.begin(self.access, (self.receipt.message_id,), processor_version=mode)
            if code == 87:
                self.assertIsNone(retry.token)
                self.assertEqual(retry.status.candidate_count, 1)
                self.assertEqual(retry.status.completed_watermark, retry.status.source_watermark)
            else:
                self.assertEqual(retry.status.attempt, 2)
                self.assertIsNone(retry.status.completed_watermark)
                self.assertEqual(workflow.candidates(self.access, retry.status.run_id), ())
                assert retry.token is not None
                draft = CandidateDraft("synthetic recovered candidate", "user_statement", (
                    EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text),))
                result = workflow.complete(self.access, retry.status.run_id, retry.token, (draft,))
                self.assertEqual(result.candidate_count, 1)
                self.assertEqual(result.completed_watermark, result.source_watermark)

    def test_roles_projection_and_versions_remain_candidates_and_do_not_grant_truth(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)
        for index, (role, provenance) in enumerate([("user", "quoted"), ("assistant", "assistant_generated"), ("user", "roleplay")], 2):
            event = replace(self.event, role=role, source=replace(self.event.source, event=str(index)), text="合成引文或扮演：不喜欢茶。")
            receipt = self.runtime.submit(self.access, event)
            lease = workflow.begin(self.access, (receipt.message_id,), processor_version="synthetic-v1")
            assert lease.token is not None
            span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(event.text), event.text)
            draft = CandidateDraft("合成否定信息", provenance, (span,), disputed=True)
            workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
            candidate = workflow.candidates(self.access, lease.status.run_id)[0]
            self.assertEqual((candidate.status, candidate.draft.provenance), ("disputed", provenance))
        protected = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="5"), sensitive=True))
        lease = workflow.begin(self.access, (protected.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None
        self.assertFalse(lease.sources[0].message.receipt.original_saved)
        projected = lease.sources[0].message.event.text
        draft = CandidateDraft("synthetic invented secret", "user_statement", (
            EvidenceSpan(protected.message_id, 1, "projection", 1, 0, len(projected), projected),))
        with self.assertRaisesRegex(MemoryFailure, "protected_candidate"):
            workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, ()).candidate_count, 0)
        partial = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="6"), completion="partial"))
        with self.assertRaisesRegex(MemoryFailure, "unsupported_extraction_source"):
            workflow.begin(self.access, (partial.message_id,), processor_version="synthetic-v1")

    def test_changed_evidence_rejects_old_inflight_output_before_any_payload_is_stored(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        evidence = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="2"), text="合成用户纠正旧记录"))
        self.runtime.change(self.access, Change("correct", self.receipt.message_id, 1, "correct", evidence.message_id, "synthetic correction"))
        draft = CandidateDraft("stale-output-must-never-save", "user_statement", (
            EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text),))
        assert lease.token is not None
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        self.assertEqual(workflow.status(self.access, lease.status.run_id).state, "stale")
        self.assertEqual(workflow.candidates(self.access, lease.status.run_id), ())
        for path in self.config.parent.iterdir():
            if path.is_file():
                self.assertNotIn(b"stale-output-must-never-save", path.read_bytes())

    def test_sqlite_full_rolls_back_all_candidates_and_success_state_then_allows_retry(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan

        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None
        span = EvidenceSpan(self.receipt.message_id, 1, "original", 1, 0, 10, self.event.text)
        drafts = tuple(CandidateDraft(str(index) + "x" * 4000, "user_statement", (span,)) for index in range(32))
        real_connect = sqlite3.connect

        def limited(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            pages = int(db.execute("PRAGMA page_count").fetchone()[0])
            db.execute(f"PRAGMA max_page_count={pages}")
            return db

        with patch("sqlite3.connect", limited):
            with self.assertRaisesRegex(MemoryFailure, "storage_full"):
                workflow.complete(self.access, lease.status.run_id, lease.token, drafts)
        status = workflow.status(self.access, lease.status.run_id)
        self.assertEqual((status.state, status.candidate_count, status.completed_watermark), ("running", 0, None))
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, ()).state, "succeeded")

    def test_every_operation_authenticates_and_a_peer_cannot_use_the_owners_lease(self) -> None:
        from evernook import CandidateWorkflow

        workflow = CandidateWorkflow(self.runtime)
        lease = workflow.begin(self.access, (self.receipt.message_id,), processor_version="synthetic-v1")
        assert lease.token is not None
        peer = pair_client(self.config, client_id="peer", entry="peer-entry", access_path=self.config.parent / "mcp-access-peer.json")
        with self.assertRaisesRegex(MemoryFailure, "invalid_candidate_lease"):
            workflow.complete(peer, lease.status.run_id, lease.token, ())
        with self.assertRaisesRegex(MemoryFailure, "invalid_candidate_lease"):
            workflow.fail(peer, lease.status.run_id, lease.token)
        for bad, code in [(replace(self.access, token="wrong"), "unauthorized"),
                          (replace(self.access, contract_version=2), "incompatible_contract")]:
            operations: list[Callable[[], object]] = [lambda: workflow.begin(bad, (self.receipt.message_id,), processor_version="synthetic-v1"),
                          lambda: workflow.complete(bad, lease.status.run_id, lease.token or "", ()),
                          lambda: workflow.fail(bad, lease.status.run_id, lease.token or ""),
                          lambda: workflow.status(bad, lease.status.run_id),
                          lambda: workflow.candidates(bad, lease.status.run_id)]
            for operation in operations:
                with self.assertRaisesRegex(MemoryFailure, code):
                    operation()
        self.assertEqual(workflow.status(peer, lease.status.run_id).state, "running")
        self.assertEqual(workflow.complete(self.access, lease.status.run_id, lease.token, ()).state, "succeeded")
