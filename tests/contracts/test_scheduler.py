from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from evernook import (CandidateDraft, CandidateLease, ConsolidationDraft, ConsolidationWorkflow, Event, EvidenceSpan, HARNESS_DEFAULT_SCOPES,
                      MemoryFailure, MemoryRuntime, Source, initialize, pair_client)
from evernook.scheduler import ConsolidationScheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-scheduler-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, _ = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.access = self.runtime.access_from_file(self.owner_path)
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.worker_path = self.config.parent / "mcp-access-worker.json"
        self.worker = pair_client(self.config, client_id="worker", entry="worker", access_path=self.worker_path,
                                  scopes=("recall", "capture", "maintain", "accept"))
        self.workflow = ConsolidationWorkflow(self.runtime)
        self.scheduler = ConsolidationScheduler(self.runtime, self.workflow)
        self.number = 0

    def submit(self, text: str, *, session: str = "s1") -> str:
        self.number += 1
        event = Event(Source("worker", session, "main", str(self.number)), "user", self.number,
                      "2026-10-30T10:00:00+08:00", "complete", text)
        return self.runtime.submit(self.worker, event).message_id

    def topic_provider(self, labels: dict[str, str]) -> Callable[[CandidateLease], tuple[ConsolidationDraft, ...]]:
        def provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            drafts = []
            for source in lease.sources:
                text = source.message.event.text
                span = EvidenceSpan(source.message.receipt.message_id, source.state_version, "original",
                                    source.message.receipt.content_version, 0, len(text), text)
                drafts.append(ConsolidationDraft("topic", labels.get(source.message.receipt.message_id,
                                                                 text[:12]), (span,), confidence=0.5))
            return tuple(drafts)
        return provider

    def test_trigger_processes_pending_jobs_once_and_reports_remaining(self) -> None:
        first, second, third = (self.submit(f"合成：调度来源{index}") for index in range(3))
        self.scheduler.configure(self.access, max_sources_per_run=2)
        tick = self.scheduler.trigger(self.access, reason="session_end",
                                      provider=self.topic_provider({first: "来源一话题", second: "来源二话题"}))
        self.assertIsNone(tick.skipped)
        self.assertEqual(len(tick.run_ids), 1)
        self.assertEqual(tick.processed, 2)
        self.assertEqual(tick.remaining, 1)
        status = self.scheduler.status(self.access)
        self.assertEqual(status.processed, 2)
        queue = self.workflow.queue(self.access)
        self.assertEqual(len(queue.rows), 2)
        second_tick = self.scheduler.trigger(self.access, reason="idle", provider=self.topic_provider({third: "来源三话题"}))
        self.assertEqual(second_tick.processed, 1)
        self.assertEqual(second_tick.remaining, 0)
        self.assertEqual(self.scheduler.status(self.access).processed, 3)
        replay = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertEqual((replay.processed, replay.remaining), (0, 0))
        self.assertEqual(replay.run_ids, ())

    def test_pause_blocks_trigger_until_resumed(self) -> None:
        self.submit("合成：暂停测试来源")
        self.scheduler.pause(self.access, paused=True)
        self.assertTrue(self.scheduler.status(self.access).paused)
        tick = self.scheduler.trigger(self.access, reason="idle", provider=self.topic_provider({}))
        self.assertEqual(tick.skipped, "paused")
        self.assertEqual(tick.processed, 0)
        self.scheduler.pause(self.access, paused=False)
        resumed = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertIsNone(resumed.skipped)
        self.assertEqual(resumed.processed, 1)

    def test_budget_limits_runs_per_trigger_and_leftover_is_reported(self) -> None:
        for index in range(4):
            self.submit(f"合成：预算来源{index}")
        self.scheduler.configure(self.access, max_sources_per_run=1, max_runs_per_trigger=2)
        tick = self.scheduler.trigger(self.access, reason="idle", provider=self.topic_provider({}))
        self.assertEqual((len(tick.run_ids), tick.processed, tick.remaining), (2, 2, 2))
        followup = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertEqual((followup.processed, followup.remaining), (2, 0))
        with self.assertRaisesRegex(MemoryFailure, "invalid_scheduler_config"):
            self.scheduler.configure(self.access, max_sources_per_run=0)
        with self.assertRaisesRegex(MemoryFailure, "invalid_scheduler_reason"):
            self.scheduler.trigger(self.access, reason="cron", provider=self.topic_provider({}))

    def test_session_end_mark_records_boundaries(self) -> None:
        marked = self.scheduler.session_end(self.worker, entry="worker", session="s1")
        self.assertTrue(marked["marked"])
        status = self.scheduler.status(self.access)
        self.assertEqual(status.session_marks, 1)
        self.scheduler.session_end(self.worker, entry="worker", session="s1")
        self.assertEqual(self.scheduler.status(self.access).session_marks, 2)
        self.assertTrue(self.scheduler.session_end(self.harness, entry="harness", session="s2")["marked"])
        self.assertEqual(self.scheduler.status(self.access).session_marks, 3)
        with self.assertRaisesRegex(MemoryFailure, "unauthorized_source"):
            self.scheduler.session_end(self.harness, entry="worker", session="s3")
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.scheduler.trigger(self.harness, reason="idle", provider=self.topic_provider({}))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.scheduler.pause(self.harness, paused=True)

    def test_abandoned_lease_resumes_without_duplicate_drafts(self) -> None:
        first = self.submit("合成：补跑来源")
        stale = self.workflow.begin(self.worker, (first,), processor_version="consolidation-scheduler-v1",
                                    lease_seconds=1)
        assert stale.token is not None
        time.sleep(1.2)
        tick = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({first: "补跑后话题"}))
        self.assertEqual(tick.processed, 1)
        rows = self.workflow.queue(self.access).rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].label, "补跑后话题")
        replay = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertEqual((replay.processed, len(self.workflow.queue(self.access).rows)), (0, 1))

    def test_provider_generic_exception_fails_lease_and_propagates(self) -> None:
        source = self.submit("合成：通用异常来源")
        captured: dict[str, str] = {}
        def broken(lease: Any) -> tuple[ConsolidationDraft, ...]:
            captured["run_id"] = lease.status.run_id
            raise ValueError("synthetic provider bug")
        with self.assertRaises(ValueError):
            self.scheduler.trigger(self.access, reason="manual", provider=broken)
        run = self.workflow.status(self.access, captured["run_id"])
        self.assertEqual((run.state, run.error_code), ("failed", "extractor_failed"))
        self.assertEqual(self.workflow.queue(self.access).rows, ())

    def test_undraftable_head_is_skipped_and_younger_sources_still_consolidate(self) -> None:
        self.runtime.submit(self.worker, Event(Source("worker", "s1", "main", "sys-1"), "system", 1,
                                               "2026-10-30T10:00:00+08:00", "complete", "合成：系统消息"))
        user = self.submit("合成：系统头之后可沉淀来源")
        self.scheduler.configure(self.access, max_sources_per_run=1)
        first = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertEqual((first.processed, first.remaining), (1, 1))
        self.assertEqual(self.workflow.queue(self.access).rows, ())
        second = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({user: "系统后话题"}))
        self.assertEqual((second.processed, second.remaining), (1, 0))
        self.assertEqual([row.label for row in self.workflow.queue(self.access).rows], ["系统后话题"])

    def test_provider_returning_no_drafts_completes_run_and_skips_batch(self) -> None:
        self.submit("合成：空批次来源")
        tick = self.scheduler.trigger(self.access, reason="manual", provider=lambda lease: ())
        self.assertEqual((tick.processed, tick.remaining), (1, 0))
        self.assertEqual(self.workflow.queue(self.access).rows, ())
        self.assertEqual(self.workflow.status(self.access, tick.run_ids[0]).state, "succeeded")
        replay = self.scheduler.trigger(self.access, reason="manual", provider=lambda lease: ())
        self.assertEqual((replay.processed, replay.run_ids), (0, ()))

    def test_crash_between_completion_and_progress_resumes_without_assertion(self) -> None:
        text = "合成：崩溃补跑来源"
        source = self.submit(text)
        lease = self.workflow.begin(self.access, (source,), processor_version="consolidation-scheduler-v1")
        assert lease.token is not None
        span = EvidenceSpan(source, 1, "original", 1, 0, len(text), text)
        self.workflow.complete(self.access, lease.status.run_id, lease.token,
                               (ConsolidationDraft("topic", "崩溃补跑话题", (span,), confidence=0.5),))
        tick = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({}))
        self.assertEqual(tick.processed, 1)
        self.assertEqual(self.scheduler.status(self.access).processed, 1)
        self.assertEqual([row.label for row in self.workflow.queue(self.access).rows], ["崩溃补跑话题"])

    def test_failure_during_completion_rolls_back_assets_and_recovers(self) -> None:
        text = "合成：成功后失败来源"
        source = self.submit(text)
        run_ids: list[str] = []
        def provider(lease: Any) -> tuple[ConsolidationDraft, ...]:
            run_ids.append(lease.status.run_id)
            item = lease.sources[0]
            span = EvidenceSpan(item.message.receipt.message_id, item.state_version, "original",
                                item.message.receipt.content_version, 0, len(item.message.event.text), item.message.event.text)
            return (ConsolidationDraft("topic", "成功后失败话题", (span,), confidence=0.5),)
        with patch.object(ConsolidationWorkflow, "_candidate_id", side_effect=ValueError("synthetic post-completion bug")):
            with self.assertRaises(ValueError):
                self.scheduler.trigger(self.access, reason="manual", provider=provider)
        run = self.workflow.status(self.access, run_ids[0])
        self.assertEqual(run.state, "failed")
        self.assertEqual(self.scheduler.status(self.access).processed, 0)
        self.assertEqual(self.workflow.queue(self.access).rows, ())
        tick = self.scheduler.trigger(self.access, reason="manual", provider=provider)
        self.assertEqual((tick.processed, self.scheduler.status(self.access).processed), (1, 1))
        self.assertEqual([row.label for row in self.workflow.queue(self.access).rows], ["成功后失败话题"])

    def test_legacy_candidate_success_recovers_assets_only_with_matching_regenerated_drafts(self) -> None:
        text = "合成：旧候选成功但草案未落地"
        source = self.submit(text)
        lease = self.workflow.begin(self.access, (source,), processor_version="consolidation-scheduler-v1")
        assert lease.token is not None
        span = EvidenceSpan(source, 1, "original", 1, 0, len(text), text)
        self.workflow.candidates.complete(self.access, lease.status.run_id, lease.token,
            (CandidateDraft("合成原草案", "quoted", (span,)),))
        with self.assertRaisesRegex(MemoryFailure, "candidate_result_conflict"):
            self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({source: "合成不同草案"}))
        self.assertEqual(self.scheduler.status(self.access).processed, 0)
        self.assertEqual(self.workflow.queue(self.access).rows, ())
        tick = self.scheduler.trigger(self.access, reason="manual", provider=self.topic_provider({source: "合成原草案"}))
        self.assertEqual((tick.processed, tick.remaining), (1, 0))
        self.assertEqual([row.label for row in self.workflow.queue(self.access).rows], ["合成原草案"])
        self.assertEqual(self.scheduler.trigger(self.access, reason="manual").processed, 0)

    def test_cli_end_to_end_configure_trigger_status(self) -> None:
        self.submit("合成：CLI 调度来源")
        for command, payload in (("scheduler-configure", {"max_sources_per_run": 4}),
                                 ("scheduler-session-end", {"entry": "worker", "session": "s1"}),
                                 ("scheduler-trigger", {"reason": "session_end"}),
                                 ("scheduler-status", {})):
            process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                                      "--access", str(self.worker_path)],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
                cwd=Path(__file__).resolve().parents[2])
            result = json.loads(process.stdout.decode("utf-8"))
            self.assertTrue(result["ok"], result)
            if command == "scheduler-trigger":
                self.assertEqual(result["result"]["processed"], 1)
            if command == "scheduler-status":
                self.assertEqual(result["result"]["processed"], 1)
                self.assertEqual(result["result"]["session_marks"], 1)


if __name__ == "__main__":
    unittest.main()
