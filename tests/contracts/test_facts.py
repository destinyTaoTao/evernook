from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import json
import subprocess
import sys
import tempfile
import unittest

from evernook import CandidateDraft, CandidateWorkflow, Change, Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.facts import FactDecision, FactRecord, FactRegistry


class FactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-facts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.facts = FactRegistry(self.runtime)
        self.workflow = CandidateWorkflow(self.runtime)
        self.number = 0

    def candidate(self, text: str, *, role: str = "user", disputed: bool = False, occurred_at: str | None = "2025-01-01T12:00:00+08:00") -> tuple[str, str]:
        self.number += 1
        event = Event(Source("synthetic", "session", "main", str(self.number)), role, self.number, occurred_at, "complete", text)
        receipt = self.runtime.submit(self.access, event)
        lease = self.workflow.begin(self.access, (receipt.message_id,), processor_version="synthetic-explicit-v1")
        assert lease.token is not None
        span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)
        draft = CandidateDraft(text, "user_statement" if role == "user" else "assistant_generated", (span,), disputed)
        self.workflow.complete(self.access, lease.status.run_id, lease.token, (draft,))
        candidate = self.workflow.candidates(self.access, lease.status.run_id)[0]
        return candidate.candidate_id, receipt.message_id

    def decide(self, request_id: str, text: str, revision: int, kind: str = "assert", *, replaces: FactRecord | None = None,
               valid_from: str | None = "2020-01-01T00:00:00+08:00", subject: str = "synthetic-person-a",
               resolves: tuple[str, ...] = ()) -> FactRecord:
        candidate, evidence = self.candidate(text)
        return self.facts.decide(self.access, FactDecision(request_id, subject, "residence", candidate, evidence,
            revision, kind, valid_from, replaces_id=replaces.fact_id if replaces else None, resolves=resolves))

    def test_correction_rewrites_claim_not_real_world_time_and_knowledge_cutoff_retains_prior_belief(self) -> None:
        old = self.decide("synthetic-old", "合成：小林住杭州", 0)
        corrected = self.decide("synthetic-correction", "合成：先前说错，小林住上海", 1, "correction", replaces=old)
        view = self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2021-02-01T00:00:00+08:00")
        self.assertEqual(view.fact, corrected)
        previous = self.facts.query(self.access, old.subject_id, old.predicate,
            valid_at="2021-02-01T00:00:00+08:00", known_at=old.recorded_at)
        self.assertEqual(previous.fact, old)
        self.assertEqual(previous.knowledge_revision, 1)
        self.assertEqual(corrected.valid_from, old.valid_from)
        self.assertNotEqual(corrected.learned_at, corrected.recorded_at)

    def test_real_change_closes_old_period_while_plan_never_replaces_actual_and_can_be_fulfilled_explicitly(self) -> None:
        old = self.decide("synthetic-home", "合成：小林住杭州", 0)
        moved = self.decide("synthetic-move", "合成：小林搬到上海", 1, "change", replaces=old, valid_from="2023-06-01T00:00:00+08:00")
        plan = self.decide("synthetic-plan", "合成：小林计划搬到成都", 2, "plan", valid_from="2024-06-01T00:00:00+08:00")
        past = self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2022-01-01T00:00:00+08:00")
        future = self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2035-01-01T00:00:00+08:00")
        self.assertEqual(past.fact, old)
        self.assertEqual(past.effective_until, moved.valid_from)
        self.assertEqual(future.fact, moved)
        self.assertEqual(future.plans, (plan,))
        actual = self.decide("synthetic-fulfilled", "合成：小林已搬到成都", 3, "change", replaces=moved,
            valid_from="2025-06-01T00:00:00+08:00", resolves=(plan.fact_id,))
        now = self.facts.query(self.access, old.subject_id, old.predicate)
        self.assertEqual(now.fact, actual)
        self.assertEqual(now.plans, ())

    def test_correcting_past_period_after_later_move_does_not_change_current_period(self) -> None:
        old = self.decide("synthetic-past", "合成：早年住杭州", 0)
        moved = self.decide("synthetic-current", "合成：后来搬上海", 1, "change", replaces=old, valid_from="2023-01-01T00:00:00+08:00")
        corrected = self.decide("synthetic-past-corrected", "合成：早年实际住苏州", 2, "correction", replaces=old)
        past = self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2021-01-01T00:00:00+08:00")
        self.assertEqual(past.fact, corrected)
        self.assertEqual(past.effective_until, moved.valid_from)
        self.assertEqual(self.facts.query(self.access, old.subject_id, old.predicate).fact, moved)

    def test_ambiguous_subjects_and_dispute_require_explicit_resolution_without_last_writer_winning(self) -> None:
        one = self.decide("synthetic-one", "合成：同名小林甲住杭州", 0)
        other = self.decide("synthetic-other", "合成：同名小林乙住成都", 0, subject="synthetic-person-b")
        dispute = self.decide("synthetic-dispute", "合成：小林甲住上海，待澄清", 1, "disputed")
        view = self.facts.query(self.access, one.subject_id, one.predicate)
        self.assertEqual(view.state, "disputed")
        self.assertTrue(view.clarification_required)
        self.assertEqual(view.fact, one)
        self.assertEqual(view.disputes, (dispute,))
        self.assertEqual(self.facts.query(self.access, other.subject_id, other.predicate).fact, other)
        with self.assertRaisesRegex(MemoryFailure, "fact_conflict_requires_resolution"):
            self.decide("synthetic-no-lww", "合成：新记录不能隐式覆盖", 2)
        resolved = self.decide("synthetic-resolve", "合成：确认甲的原陈述正确", 2, "correction", replaces=one, resolves=(dispute.fact_id,))
        self.assertEqual(self.facts.query(self.access, one.subject_id, one.predicate).fact, resolved)
        self.assertFalse(self.facts.query(self.access, one.subject_id, one.predicate).clarification_required)

    def test_concurrent_old_revision_cannot_overwrite_newer_decision_and_retries_are_idempotent(self) -> None:
        old = self.decide("synthetic-initial", "合成：原始住址", 0)
        candidate, evidence = self.candidate("合成：更正住址")
        request = FactDecision("synthetic-cas", old.subject_id, old.predicate, candidate, evidence, 1,
            "correction", old.valid_from, replaces_id=old.fact_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.facts.decide(self.access, request), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.facts.decide(self.access, request), results[0])
        with self.assertRaisesRegex(MemoryFailure, "fact_revision_conflict"):
            self.facts.decide(self.access, replace(request, request_id="synthetic-stale-task"))
        with self.assertRaisesRegex(MemoryFailure, "fact_request_conflict"):
            self.facts.decide(self.access, replace(request, expected_revision=2))
        self.assertEqual(self.facts.query(self.access, old.subject_id, old.predicate).revision, 2)

    def test_suppressed_latest_source_does_not_resurrect_old_fact_even_for_knowledge_history(self) -> None:
        old = self.decide("synthetic-before", "合成：旧住所杭州", 0)
        moved = self.decide("synthetic-after", "合成：新住所上海", 1, "change", replaces=old, valid_from="2023-01-01T00:00:00+08:00")
        _, control = self.candidate("合成：暂停新住所的普通召回")
        self.runtime.change(self.access, Change("synthetic-suppress", moved.sources[0].message_id, 1, "suppress", control, "synthetic"))
        view = self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2025-01-01T00:00:00+08:00")
        self.assertIsNone(view.fact)
        self.assertEqual(view.state, "source_unavailable")
        self.assertIn(moved.fact_id, view.unavailable_ids)
        self.assertEqual(self.facts.query(self.access, old.subject_id, old.predicate, valid_at="2022-01-01T00:00:00+08:00").fact, old)

    def test_purged_successor_does_not_resurrect_old_fact_after_restart(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow

        old = self.decide("purge-before", "合成：旧住所杭州", 0)
        moved = self.decide("purge-after", "合成：新住所上海", 1, "change", replaces=old,
            valid_from="2023-01-01T00:00:00+08:00")
        other = self.decide("purge-other", "合成：另一主体住成都", 0, subject="synthetic-person-b")
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, "purge-successor", (moved.sources[0].message_id,))
        _, approval = self.candidate("合成：用户确认彻底删除新住所来源")
        decision = PurgeDecision(preview.request_id, approval)
        result = purge.purge(self.access, decision)

        reopened = FactRegistry(MemoryRuntime(self.config))
        current = reopened.query(self.access, old.subject_id, old.predicate)
        self.assertIsNone(current.fact)
        self.assertEqual(current.state, "source_unavailable")
        self.assertEqual(current.revision, 2)
        historical = reopened.query(self.access, old.subject_id, old.predicate,
            valid_at="2022-01-01T00:00:00+08:00")
        self.assertEqual(historical.fact, old)
        self.assertEqual(historical.effective_until, moved.valid_from)
        self.assertEqual(reopened.query(self.access, other.subject_id, other.predicate).fact, other)
        self.assertEqual(PurgeWorkflow(reopened.runtime).purge(self.access, decision), result)

    def test_purged_correction_stays_unavailable_through_interrupted_cleanup_and_retries(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow

        old = self.decide("erased-old", "合成：原先说错的住所", 0)
        corrected = self.decide("erased-correction", "合成：更正后的住所", 1, "correction", replaces=old)
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, "purge-correction", (corrected.confirmation_id,))
        _, approval = self.candidate("合成：用户确认删除纠错来源")
        decision = PurgeDecision(preview.request_id, approval)
        with closing(sqlite3.connect(self.runtime.root / "memory.sqlite3")) as db:
            db.execute("""CREATE TRIGGER synthetic_fact_purge_interruption BEFORE DELETE ON messages
                BEGIN SELECT RAISE(ABORT,'synthetic purge interruption'); END""")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            purge.purge(self.access, decision)
        reopened = FactRegistry(MemoryRuntime(self.config))
        self.assertIsNone(reopened.query(self.access, old.subject_id, old.predicate).fact)
        with closing(sqlite3.connect(self.runtime.root / "memory.sqlite3")) as db:
            db.execute("DROP TRIGGER synthetic_fact_purge_interruption")
        result = PurgeWorkflow(reopened.runtime).purge(self.access, decision)
        self.assertEqual(PurgeWorkflow(MemoryRuntime(self.config)).purge(self.access, decision), result)
        for valid_at in (None, "2021-01-01T00:00:00+08:00"):
            view = reopened.query(self.access, old.subject_id, old.predicate, valid_at=valid_at)
            self.assertIsNone(view.fact)
            self.assertEqual(view.state, "source_unavailable")
            self.assertEqual((view.revision, view.knowledge_revision), (2, 2))
        before = reopened.query(self.access, old.subject_id, old.predicate, known_at=old.recorded_at)
        self.assertEqual(before.fact, old)
        candidate, evidence = self.candidate("合成：删除后重新提供的住所")
        with self.assertRaisesRegex(MemoryFailure, "fact_source_purged"):
            reopened.decide(self.access, FactDecision(corrected.fact_id, old.subject_id, old.predicate,
                candidate, evidence, 2, "correction", old.valid_from, replaces_id=old.fact_id))
        with self.assertRaisesRegex(MemoryFailure, "fact_revision_conflict"):
            reopened.decide(self.access, FactDecision("erased-stale", old.subject_id, old.predicate,
                candidate, evidence, 1, "correction", old.valid_from, replaces_id=old.fact_id))
        new = reopened.decide(self.access, FactDecision("erased-new", old.subject_id, old.predicate,
            candidate, evidence, 2, "change", "2024-01-01T00:00:00+08:00", replaces_id=corrected.fact_id))
        self.assertEqual((new.revision, reopened.query(self.access, old.subject_id, old.predicate).fact), (3, new))

    def test_unknown_time_not_inferred_from_import_and_unknown_knowledge_cutoff_has_no_fact(self) -> None:
        candidate, evidence = self.candidate("合成：导入记录时间未知", occurred_at=None)
        record = self.facts.decide(self.access, FactDecision("synthetic-unknown", "synthetic-person-a", "residence", candidate, evidence, 0, "assert"))
        current = self.facts.query(self.access, record.subject_id, record.predicate)
        self.assertEqual(current.state, "unknown_time")
        self.assertEqual(current.fact, record)
        self.assertIsNone(record.learned_at)
        past = self.facts.query(self.access, record.subject_id, record.predicate, valid_at="2020-01-01T00:00:00+08:00")
        self.assertEqual(past.state, "unknown_time")
        self.assertIsNone(past.fact)
        not_known = self.facts.query(self.access, record.subject_id, record.predicate, known_at="2000-01-01T00:00:00+08:00")
        self.assertIsNone(not_known.fact)
        self.assertEqual(not_known.knowledge_revision, 0)

    def test_assistant_claims_stale_candidates_and_invalid_time_changes_are_refused(self) -> None:
        assistant, evidence = self.candidate("合成：助手编造住所", role="assistant")
        with self.assertRaisesRegex(MemoryFailure, "fact_requires_user_statement"):
            self.facts.decide(self.access, FactDecision("synthetic-assistant", "synthetic-person-a", "residence", assistant, evidence, 0, "assert"))
        old = self.decide("synthetic-base", "合成：当前住所", 0)
        candidate, user = self.candidate("合成：错误时间更正")
        request = FactDecision("synthetic-invalid-time", old.subject_id, old.predicate, candidate, user, 1, "correction",
            "2021-01-01T00:00:00+08:00", replaces_id=old.fact_id)
        with self.assertRaisesRegex(MemoryFailure, "correction_must_preserve_time"):
            self.facts.decide(self.access, request)
        with self.assertRaisesRegex(MemoryFailure, "non_monotonic_fact_change"):
            self.facts.decide(self.access, replace(request, kind="change", valid_from="2019-01-01T00:00:00+08:00"))
        _, control = self.candidate("合成：暂停来源")
        self.runtime.change(self.access, Change("synthetic-stale", user, 1, "suppress", control, "synthetic"))
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            self.facts.decide(self.access, replace(request, valid_from=old.valid_from))

    def test_rolled_back_fact_write_leaves_no_revision_or_partial_decision_and_restart_retry_works(self) -> None:
        candidate, evidence = self.candidate("合成：事务回滚住址")
        request = FactDecision("synthetic-rollback", "synthetic-person-a", "residence", candidate, evidence, 0, "assert")
        with self.runtime._connection() as db:
            FactRegistry._schema(db)
            db.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON fact_heads_v1 BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.facts.decide(self.access, request)
        self.assertEqual(self.facts.query(self.access, request.subject_id, request.predicate).revision, 0)
        with closing(sqlite3.connect(self.runtime.root / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DROP TRIGGER synthetic_fail")
        restarted = FactRegistry(MemoryRuntime(self.config))
        saved = restarted.decide(self.access, request)
        self.assertEqual(restarted.decide(self.access, request), saved)

    def test_backwards_clock_step_cannot_fabricate_a_knowledge_view_that_never_existed(self) -> None:
        from datetime import datetime as real_datetime, timezone as real_timezone
        from unittest.mock import patch

        from evernook.runtime import _time

        old = self.decide("synthetic-watermark", "合成：小林住杭州", 0)
        earlier = real_datetime(2020, 1, 1, tzinfo=real_timezone.utc)

        class _BackwardsClock:
            @staticmethod
            def now(tz: object = None) -> real_datetime:
                return earlier

        with patch("evernook.facts.datetime", _BackwardsClock):
            corrected = self.decide("synthetic-watermark-correction", "合成：先前说错，住上海", 1, "correction", replaces=old)
        # The knowledge watermark is monotonic per subject/predicate even when the wall clock regresses.
        self.assertGreaterEqual(_time(corrected.recorded_at), _time(old.recorded_at))
        between = self.facts.query(self.access, old.subject_id, old.predicate,
            known_at=real_datetime.fromtimestamp(_time(old.recorded_at) - 1800, real_timezone.utc).isoformat())
        self.assertIsNone(between.fact)
        self.assertEqual(between.knowledge_revision, 0)
        current = self.facts.query(self.access, old.subject_id, old.predicate)
        self.assertEqual(current.fact, corrected)
        self.assertEqual(current.knowledge_revision, 2)

    def test_real_cli_decision_and_query_preserve_unknown_time_and_safe_source_receipts(self) -> None:
        from dataclasses import asdict
        access_file = self.runtime.root / "mcp-access-facts.json"
        pair_client(self.config, client_id=self.access.client_id, entry="synthetic", access_path=access_file)
        candidate, evidence = self.candidate("合成：CLI确认的住址")
        request = FactDecision("synthetic-cli", "synthetic-person-a", "residence", candidate, evidence, 0, "assert")
        base = [sys.executable, "-m", "evernook.cli"]
        paths = ["--config", str(self.config), "--access", str(access_file)]
        result = subprocess.run([*base, "fact-decide", *paths], input=json.dumps(asdict(request)).encode(), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        saved = json.loads(result.stdout)["result"]
        self.assertEqual(saved["fact_id"], request.request_id)
        queried = subprocess.run([*base, "fact-query", *paths], input=json.dumps({"subject_id": request.subject_id,
            "predicate": request.predicate}).encode(), capture_output=True, timeout=30)
        self.assertEqual(queried.returncode, 0, queried.stderr.decode())
        view = json.loads(queried.stdout)["result"]
        self.assertEqual(view["state"], "unknown_time")
        self.assertEqual(view["fact"]["text"], "合成：CLI确认的住址")


if __name__ == "__main__":
    unittest.main()
