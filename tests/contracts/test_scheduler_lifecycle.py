from __future__ import annotations

from pathlib import Path
from contextlib import closing
import json
import subprocess
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from evernook import CandidateLease, Change, ConsolidationDraft, ConsolidationWorkflow, Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.scheduler import ConsolidationScheduler, extractive_provider
from evernook.profile import ProfileRegistry
from evernook.purge import PurgeDecision, PurgeWorkflow


class SchedulerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-scheduler-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.scheduler = ConsolidationScheduler(self.runtime)

    def test_backlog_age_and_processing_coverage_survive_restart_and_keep_index_lag_explicit(self) -> None:
        for index in range(2):
            self.runtime.submit(self.access, Event(Source("synthetic", "session", "main", str(index)),
                "user", index, None, "complete", "合成：待处理主题" + str(index)))
        before = self.scheduler.status(self.access)
        self.assertEqual(before.pending, 2)
        self.assertIsNotNone(before.oldest_pending_seconds)
        assert before.oldest_pending_seconds is not None
        self.assertGreaterEqual(before.oldest_pending_seconds, 0)
        assert before.lexical is not None
        self.assertEqual(before.lexical.state, "not_built")
        self.assertEqual(before.lexical.pending_messages, 2)
        self.scheduler.trigger(self.access, reason="manual")
        after = ConsolidationScheduler(MemoryRuntime(self.config)).status(self.access)
        self.assertEqual(after.pending, 0)
        self.assertIsNone(after.oldest_pending_seconds)
        self.assertEqual(after.processed, 2)
        self.assertEqual(after.covered_messages, 2)
        assert after.lexical is not None
        self.assertEqual(after.lexical.pending_messages, 2)

    def test_saved_threshold_idle_delay_and_pause_gate_real_trigger_calls_after_restart(self) -> None:
        self.scheduler.configure(self.access, min_pending=3, idle_seconds=100, max_trigger_seconds=2)
        scheduler = ConsolidationScheduler(MemoryRuntime(self.config))
        for index in range(2):
            self.runtime.submit(self.access, Event(Source("synthetic", "gates", "main", str(index)),
                "user", index, None, "complete", "合成：门槛来源" + str(index)))
        self.assertEqual(scheduler.trigger(self.access, reason="turn_end").skipped, "threshold")
        self.runtime.submit(self.access, Event(Source("synthetic", "gates", "main", "third"),
            "user", 3, None, "complete", "合成：第三条来源"))
        self.assertEqual(scheduler.trigger(self.access, reason="idle").skipped, "idle_wait")
        self.assertEqual(scheduler.trigger(self.access, reason="turn_end").processed, 3)
        saved = scheduler.pause(self.access, paused=True)
        self.assertEqual((saved.min_pending, saved.idle_seconds, saved.max_trigger_seconds), (3, 100, 2))
        self.assertEqual(ConsolidationScheduler(MemoryRuntime(self.config)).trigger(self.access, reason="startup").skipped, "paused")

    def test_another_hosts_foreground_lease_defers_work_without_granting_maintenance_rights(self) -> None:
        host = pair_client(self.config, client_id="synthetic-host", entry="host",
            access_path=self.config.parent / "mcp-access-host.json", scopes=("capture", "recall"))
        self.runtime.submit(self.access, Event(Source("synthetic", "backlog", "main", "1"),
            "user", 1, None, "complete", "合成：跨入口后台来源"))
        self.scheduler.foreground(host, session="running", active=True, ttl_seconds=30)
        restarted = ConsolidationScheduler(MemoryRuntime(self.config))
        self.assertEqual(restarted.trigger(self.access, reason="startup").skipped, "foreground")
        self.assertEqual(restarted.status(self.access).foreground_sessions, 1)
        self.scheduler.foreground(self.access, session="running", active=False)
        self.assertEqual(restarted.trigger(self.access, reason="manual").skipped, "foreground")
        self.scheduler.foreground(host, session="running", active=False)
        self.assertEqual(restarted.trigger(self.access, reason="startup").processed, 1)

    def test_new_foreground_during_processing_yields_before_publication_and_keeps_work_retryable(self) -> None:
        self.runtime.submit(self.access, Event(Source("synthetic", "preemption", "main", "1"),
            "user", 1, None, "complete", "合成：处理期间前台开始"))
        def provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            self.scheduler.foreground(self.access, session="interactive", active=True)
            return extractive_provider(lease)
        tick = self.scheduler.trigger(self.access, reason="manual", provider=provider)
        self.assertEqual((tick.skipped, tick.processed, tick.remaining), ("foreground", 0, 1))
        self.assertEqual(ConsolidationWorkflow(self.runtime).queue(self.access).rows, ())
        self.scheduler.foreground(self.access, session="interactive", active=False)
        self.assertEqual(self.scheduler.trigger(self.access, reason="manual").processed, 1)
        self.assertEqual(len(ConsolidationWorkflow(self.runtime).queue(self.access).rows), 1)

    def test_saved_time_budget_refuses_late_provider_output_without_advancing_progress(self) -> None:
        self.scheduler.configure(self.access, max_trigger_seconds=0.1)
        self.runtime.submit(self.access, Event(Source("synthetic", "budget", "main", "1"),
            "user", 1, None, "complete", "合成：超时后不能提交"))
        expired = False
        now = time.monotonic()
        def slow_provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            nonlocal expired
            expired = True
            return extractive_provider(lease)
        with patch("evernook.scheduler.time.monotonic", side_effect=lambda: now + (1 if expired else 0)):
            tick = self.scheduler.trigger(self.access, reason="manual", provider=slow_provider)
        self.assertEqual((tick.skipped, tick.processed, tick.remaining), ("time_budget", 0, 1))
        self.assertEqual(ConsolidationWorkflow(self.runtime).queue(self.access).rows, ())
        self.assertEqual(self.scheduler.status(self.access).failure_reasons.get("timed_out"), 1)
        self.scheduler.configure(self.access, max_trigger_seconds=2)
        self.assertEqual(self.scheduler.trigger(self.access, reason="startup").processed, 1)

    def test_capture_only_cli_can_announce_foreground_but_cannot_run_maintenance(self) -> None:
        access_file = self.config.parent / "mcp-access-host-cli.json"
        pair_client(self.config, client_id="host-cli", entry="host-cli", access_path=access_file, scopes=("capture", "recall"))
        def invoke(command: str, value: object) -> dict[str, Any]:
            result = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                "--access", str(access_file)], input=json.dumps(value).encode(), capture_output=True, timeout=30)
            self.assertEqual(result.stderr, b"")
            payload: dict[str, Any] = json.loads(result.stdout)
            return payload
        marked = invoke("scheduler-foreground", {"session": "native", "active": True})
        self.assertTrue(marked["ok"], marked)
        self.assertEqual(self.scheduler.status(self.access).foreground_sessions, 1)
        denied = invoke("scheduler-trigger", {"reason": "idle"})
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "permission_denied")

    def test_expired_activity_and_abandoned_lease_are_reclaimed_by_a_different_worker(self) -> None:
        self.runtime.submit(self.access, Event(Source("synthetic", "crashed", "main", "1"),
            "user", 1, None, "complete", "合成：缺失最终回调仍需处理")).message_id
        self.scheduler.foreground(self.access, session="crashed-host", active=True, ttl_seconds=1)
        workflow = ConsolidationWorkflow(self.runtime)
        owner_file = self.config.parent / 'mcp-access-crashing-worker.json'
        pair_client(self.config, client_id='synthetic', entry='synthetic', access_path=owner_file)
        script = ("import json,os,sys; from pathlib import Path; from evernook import MemoryRuntime; "
            "from evernook.scheduler import ConsolidationScheduler; r=MemoryRuntime(Path(sys.argv[1])); "
            "a=r.access_from_file(Path(sys.argv[2])); s=ConsolidationScheduler(r); "
            "s.foreground(a,session='crashed-host',active=False); "
            "s.trigger(a,reason='startup',provider=lambda lease: (print(json.dumps(lease.status.run_id),flush=True),os._exit(91)))")
        crashed = subprocess.run([sys.executable, '-c', script, str(self.config), str(owner_file)],
            capture_output=True, timeout=20)
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        run_id = json.loads(crashed.stdout)
        self.scheduler.foreground(self.access, session='crashed-host', active=True, ttl_seconds=1)
        worker = pair_client(self.config, client_id="replacement-worker", entry="replacement-worker",
            access_path=self.config.parent / "mcp-access-replacement.json", scopes=("recall", "maintain", "accept"))
        future = time.time() + 60
        with patch("evernook.scheduler.time.time", return_value=future):
            self.assertEqual(self.scheduler.status(self.access).failure_reasons.get("lease_expired"), 1)
            tick = self.scheduler.trigger(worker, reason="startup")
            self.assertEqual(tick.run_ids, (run_id,))
            self.assertEqual((tick.processed, tick.remaining), (1, 0))
        self.assertEqual(len(workflow.queue(worker).rows), 1)

    def test_success_without_progress_is_recovered_before_new_arrivals_change_the_batch(self) -> None:
        first = self.runtime.submit(self.access, Event(Source('synthetic', 'growth', 'main', '1'),
            'user', 1, None, 'complete', '合成：先前已落盘的草案')).message_id
        workflow = ConsolidationWorkflow(self.runtime)
        lease = workflow.begin(self.access, (first,), processor_version='consolidation-scheduler-v1')
        assert lease.token is not None
        workflow.complete(self.access, lease.status.run_id, lease.token, extractive_provider(lease))
        self.runtime.submit(self.access, Event(Source('synthetic', 'growth', 'main', '2'),
            'user', 2, None, 'complete', '合成：崩溃后新增的来源'))
        restarted = ConsolidationScheduler(MemoryRuntime(self.config))
        for _ in range(2):
            restarted.trigger(self.access, reason='startup')
        self.assertEqual(restarted.status(self.access).pending, 0)
        rows = workflow.queue(self.access).rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(span.message_id == first for row in rows for span in row.sources), 1)

    def test_concurrent_new_arrival_cannot_form_an_overlapping_inflight_batch(self) -> None:
        self.runtime.submit(self.access, Event(Source('synthetic', 'concurrent-growth', 'main', '1'),
            'user', 1, None, 'complete', '合成：正在处理的来源'))
        def provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            self.runtime.submit(self.access, Event(Source('synthetic', 'concurrent-growth', 'main', '2'),
                'user', 2, None, 'complete', '合成：并发新增来源'))
            with self.assertRaisesRegex(MemoryFailure, 'candidate_run_busy'):
                ConsolidationScheduler(MemoryRuntime(self.config)).trigger(self.access, reason='idle')
            return extractive_provider(lease)
        self.scheduler.trigger(self.access, reason='startup', provider=provider)
        self.scheduler.trigger(self.access, reason='startup')
        self.assertEqual(self.scheduler.status(self.access).pending, 0)
        self.assertEqual(len(ConsolidationWorkflow(self.runtime).queue(self.access).rows), 2)

    def test_reclaimed_scheduler_fences_old_completion_inside_the_asset_transaction(self) -> None:
        self.runtime.submit(self.access, Event(Source('synthetic', 'fenced', 'main', '1'),
            'user', 1, None, 'complete', '合成：旧工作进程延迟提交'))
        workflow = ConsolidationWorkflow(self.runtime)
        scheduler = ConsolidationScheduler(self.runtime, workflow)
        original = workflow.complete
        def delayed(*args: Any, **kwargs: Any) -> Any:
            with closing(sqlite3.connect(self.config.parent / 'memory.sqlite3')) as db:
                global_until = float(db.execute('SELECT expires FROM scheduler_lease_v1').fetchone()[0])
                candidate_until = float(db.execute('SELECT lease_until FROM candidate_runs_v1').fetchone()[0])
            self.assertGreater(candidate_until, global_until)
            self.runtime.submit(self.access, Event(Source('synthetic', 'fenced', 'main', '2'),
                'user', 2, None, 'complete', '合成：新工作进程接手'))
            with patch('evernook.scheduler.time.time', return_value=(global_until + candidate_until) / 2):
                ConsolidationScheduler(MemoryRuntime(self.config)).trigger(self.access, reason='startup')
                return original(*args, **kwargs)
        with patch.object(workflow, 'complete', side_effect=delayed):
            tick = scheduler.trigger(self.access, reason='startup')
        self.assertEqual(tick.skipped, 'time_budget')
        self.assertEqual(len(workflow.queue(self.access).rows), 2)

    def test_partial_source_invalidation_preserves_valid_assets_without_repeating_them(self) -> None:
        ids = tuple(self.runtime.submit(self.access, Event(Source('synthetic', 'stale-success', 'main', str(index)),
            'user', index, None, 'complete', text)).message_id
            for index, text in enumerate(('合成：仍有效来源', '合成：不再召回内容')))
        workflow = ConsolidationWorkflow(self.runtime)
        lease = workflow.begin(self.access, ids, processor_version='consolidation-scheduler-v1')
        assert lease.token is not None
        drafts = extractive_provider(lease)
        shared = ConsolidationDraft('topic', '合成：共同来源草案', tuple(span for draft in drafts for span in draft.sources))
        workflow.complete(self.access, lease.status.run_id, lease.token, drafts + (shared,))
        confirmation = self.runtime.submit(self.access, Event(Source('synthetic', 'stale-success', 'main', 'suppression'),
            'user', 3, None, 'complete', '合成：请不再召回第二条来源')).message_id
        self.runtime.change(self.access, Change('stale-success-suppress', ids[1], 1, 'suppress', confirmation, 'synthetic'))
        valid = workflow.queue(self.access).rows
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0].sources[0].message_id, ids[0])
        self.scheduler.trigger(self.access, reason='startup')
        self.scheduler.trigger(self.access, reason='startup')
        rows = workflow.queue(self.access).rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(span.message_id == ids[0] for row in rows for span in row.sources), 1)
        self.assertIn(valid[0].candidate_id, {row.candidate_id for row in rows})
        self.assertNotIn('不再召回内容', repr([row.label for row in rows]))

    def test_purge_preserves_verified_sibling_coverage_before_erasing_the_completed_run(self) -> None:
        self.scheduler.configure(self.access)
        ids = tuple(self.runtime.submit(self.access, Event(Source('synthetic', 'purge-coverage', 'main', str(index)),
            'user', index, None, 'complete', text)).message_id
            for index, text in enumerate(('合成：保留的兴趣证据', '合成：需要删除的兴趣证据')))
        workflow = ConsolidationWorkflow(self.runtime)
        workflow.configure(self.access, tier='auto')
        def provider(lease: CandidateLease) -> tuple[ConsolidationDraft, ...]:
            return tuple(ConsolidationDraft('profile_tag', draft.label, draft.sources, confidence=0.8)
                for draft in extractive_provider(lease))
        lease = workflow.begin(self.access, ids, processor_version='consolidation-scheduler-v1')
        assert lease.token is not None
        workflow.complete(self.access, lease.status.run_id, lease.token, provider(lease))
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, 'purge-coverage', (ids[1],))
        confirmation = self.runtime.submit(self.access, Event(Source('synthetic', 'purge-coverage', 'main', 'confirmation'),
            'user', 3, None, 'complete', '合成：确认按预览删除第二条兴趣证据')).message_id
        purge.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        self.assertEqual(self.scheduler.status(self.access).pending, 1)
        self.scheduler.trigger(self.access, reason='startup', provider=provider)
        # M58-20 retains legacy, unadmitted interest proposals as drafts.
        rows = [row for row in workflow.queue(self.access).rows
            if row.kind == 'profile_tag' and any(span.message_id == ids[0] for span in row.sources)]
        self.assertEqual(len(rows), 1)
        assert rows[0].proposal_id is not None
        self.assertEqual(ProfileRegistry(self.runtime).read(self.access, rows[0].proposal_id).state, 'draft')


if __name__ == "__main__":
    unittest.main()
