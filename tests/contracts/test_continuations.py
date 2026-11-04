from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from typing import Any

from evernook import Change, Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.continuations import ContinuationDraft, Continuations


class ContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-continuations-')
        self.addCleanup(temporary.cleanup)
        self.config, self.owner = initialize(Path(temporary.name) / 'ordinary', client_id='synthetic-owner', entry='owner')
        self.runtime = MemoryRuntime(self.config)
        self.host = pair_client(self.config, client_id='synthetic-dsh', entry='dsh',
            access_path=self.config.parent / 'mcp-access-dsh.json', scopes=('capture', 'recall'))
        self.registry = Continuations(self.runtime)

    def save(self, text: str, key: str, role: str = 'user') -> EvidenceSpan:
        receipt = self.runtime.submit(self.host, Event(Source('dsh', 'task-session', 'main', key),
            role, 1, None, 'complete', text))
        return EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)

    def test_restart_and_another_entry_restore_the_same_source_backed_work_without_rewriting_originals(self) -> None:
        goal = self.save('合成任务：迁移查询模块，保留原有接口。', 'goal')
        progress = self.save('合成进展：迁移实现已保存；尚未运行契约回归。', 'progress', 'assistant')
        pending = self.save('合成待办：运行契约回归，然后检查调用方。', 'pending', 'system')
        draft = ContinuationDraft('migration', 'main', 0, pending.message_id, goal, (progress,), (pending,),
            'in_progress', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        accepted = self.registry.save(self.host, 'checkpoint-1', draft)
        reader = pair_client(self.config, client_id='synthetic-pi', entry='pi',
            access_path=self.config.parent / 'mcp-access-pi.json', scopes=('capture', 'recall'))
        restarted = Continuations(MemoryRuntime(self.config))
        restored = restarted.current(reader, 'migration', 'main')
        self.assertEqual(restored, accepted)
        self.assertEqual(restored.state, 'in_progress')
        self.assertEqual(restored.goal['text'], goal.expected_text)
        self.assertEqual(restored.progress[0]['text'], progress.expected_text)
        self.assertEqual(restored.pending[0]['text'], pending.expected_text)
        self.assertEqual(restarted.save(self.host, 'checkpoint-1', draft), accepted)
        for span in (goal, progress, pending):
            stored = self.runtime.read(reader, span.message_id)
            assert stored is not None
            self.assertEqual(stored.event.text, span.expected_text)

    def test_suppressed_latest_checkpoint_never_falls_back_to_an_older_work_state(self) -> None:
        goal = self.save('合成任务：调整查询模块。', 'suppress-goal')
        old = self.save('合成进展：实现待完成。', 'old', 'assistant')
        latest = self.save('合成进展：实现已完成，测试待运行。', 'latest', 'assistant')
        draft = ContinuationDraft('search', 'main', 0, old.message_id, goal, (old,), (),
            'in_progress', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        first = self.registry.save(self.host, 'old-state', draft)
        newest = replace(draft, expected_revision=1, anchor_id=latest.message_id, progress=(latest,))
        second = self.registry.save(self.host, 'new-state', newest)
        confirmation = self.save('合成用户确认：不再召回最新进展。', 'suppress-confirm')
        self.runtime.change(self.owner, Change('suppress-progress', latest.message_id, 1, 'suppress',
            confirmation.message_id, 'synthetic confirmation'))
        restored = Continuations(MemoryRuntime(self.config)).current(self.host, 'search', 'main')
        self.assertEqual(restored.checkpoint_id, second.checkpoint_id)
        self.assertNotEqual(restored.checkpoint_id, first.checkpoint_id)
        self.assertEqual(restored.state, 'source_unavailable')
        self.assertEqual(restored.goal, {})
        self.assertEqual(restored.progress, ())
        self.assertEqual(restored.pending, ())
        self.assertEqual(self.registry.save(self.host, 'new-state', newest), restored)

    def test_expiry_and_abandonment_exclude_work_without_selecting_another_branch(self) -> None:
        goal = self.save('合成任务：迁移测试。', 'expiry-goal')
        progress = self.save('合成进展：迁移尚未结束。', 'expiry-progress', 'assistant')
        draft = ContinuationDraft('migration', 'main', 0, progress.message_id, goal, (progress,), (),
            'in_progress', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        first = self.registry.save(self.host, 'expiring', draft)
        abandoned = self.registry.save(self.host, 'abandoned', replace(draft, branch_id='discarded', state='abandoned'))
        self.assertEqual(self.registry.current(self.host, 'migration', 'main'), first)
        discarded = self.registry.current(self.host, 'migration', 'discarded')
        self.assertEqual(discarded.state, 'abandoned')
        self.assertEqual(discarded.goal, {})
        self.assertEqual(discarded.progress, ())
        with patch('evernook.continuations.time.time', return_value=time.time() + 7200):
            expired = self.registry.current(self.host, 'migration', 'main')
        self.assertEqual(expired.checkpoint_id, first.checkpoint_id)
        self.assertNotEqual(expired.checkpoint_id, abandoned.checkpoint_id)
        self.assertEqual(expired.state, 'expired')
        self.assertEqual(expired.goal, {})
        self.assertEqual(expired.pending, ())

    def test_completed_work_cannot_also_claim_pending_items_and_invalid_records_are_never_saved(self) -> None:
        goal = self.save('合成任务：迁移回归。', 'completed-goal')
        pending = self.save('合成待办：还有回归未运行。', 'completed-pending', 'assistant')
        draft = ContinuationDraft('completion', 'main', 0, pending.message_id, goal, (), (pending,),
            'completed', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(MemoryFailure, 'invalid_continuation'):
            self.registry.save(self.host, 'conflicting-completion', draft)
        with self.assertRaisesRegex(MemoryFailure, 'continuation_not_found'):
            self.registry.current(self.host, 'completion', 'main')
        complete = self.registry.save(self.host, 'completion', replace(draft, pending=()))
        self.assertEqual(complete.state, 'completed')
        self.assertEqual(complete.pending, ())

    def test_checkpoint_input_is_bounded_and_only_a_user_source_can_establish_the_goal(self) -> None:
        goal = self.save('合成任务：保留接口。', 'bounded-goal')
        guess = self.save('合成助手猜测用户想重写系统。', 'guessed-goal', 'assistant')
        draft = ContinuationDraft('bounds', 'main', 0, goal.message_id, goal, (), (),
            'unknown', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        invalid: tuple[dict[str, Any], ...] = ({'state': 'accepted_fact'}, {'expected_revision': True},
            {'progress': [goal]}, {'pending': (goal,) * 65}, {'goal': guess},
            {'goal': replace(goal, representation='projection')}, {'goal': replace(goal, start=-1)},
            {'expires_at': 'tomorrow'}, {'anchor_id': 'absent'})
        for index, values in enumerate(invalid):
            with self.subTest(values=values), self.assertRaises(MemoryFailure):
                self.registry.save(self.host, 'invalid-' + str(index), replace(draft, **values))
        with self.assertRaisesRegex(MemoryFailure, 'continuation_not_found'):
            self.registry.current(self.host, 'bounds', 'main')

    def test_invalidating_a_summarized_dependency_blocks_the_summary_even_if_its_own_source_is_active(self) -> None:
        goal = self.save('合成任务：修复接口。', 'dependency-goal')
        tool = self.save('合成工具结果：版本A尚未修复。', 'dependency-tool', 'tool')
        summary = self.save('合成压缩：接口版本A，待修复。', 'dependency-summary', 'system')
        draft = ContinuationDraft('dependency', 'main', 0, summary.message_id, goal, (summary,), (), 'unknown',
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            dependencies=((tool.message_id, 1, 1),))
        self.registry.save(self.host, 'dependency', draft)
        confirm = self.save('合成用户确认：不再召回工具结果A。', 'dependency-confirm')
        self.runtime.change(self.owner, Change('suppress-dependency', tool.message_id, 1, 'suppress', confirm.message_id, 'synthetic'))
        restored = self.registry.current(self.host, 'dependency', 'main')
        self.assertEqual(restored.state, 'source_unavailable')
        self.assertEqual(restored.progress, ())
        self.assertIn(tool.message_id, restored.omitted_sources)

    def test_independent_anchor_control_is_bound_even_when_the_anchor_is_not_quoted(self) -> None:
        goal = self.save('合成任务：核对完成状态。', 'anchor-goal')
        anchor = self.save('合成可信工作状态：本任务已完成。', 'anchor-status', 'system')
        draft = ContinuationDraft('anchor', 'main', 0, anchor.message_id, goal, (), (), 'completed',
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.registry.save(self.host, 'anchor', draft)
        confirmation = self.save('合成用户确认：不再召回该完成状态。', 'anchor-confirm')
        self.runtime.change(self.owner, Change('suppress-anchor', anchor.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        restored = self.registry.current(self.host, 'anchor', 'main')
        self.assertEqual(restored.state, 'source_unavailable')
        self.assertEqual(restored.goal, {})
        self.assertIn(anchor.message_id, restored.omitted_sources)

    def test_consumed_checkpoint_carries_its_dependency_versions_into_a_later_partial_summary(self) -> None:
        goal = self.save('合成任务：检查工具结论。', 'consumed-goal')
        tool = self.save('合成工具旧结论：采用方案甲。', 'consumed-tool', 'tool')
        first_summary = self.save('合成首次压缩：方案甲待复核。', 'consumed-first', 'system')
        first = ContinuationDraft('consumed', 'main', 0, first_summary.message_id, goal, (first_summary,), (), 'unknown',
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), dependencies=((tool.message_id, 1, 1),))
        saved = self.registry.save(self.host, 'consumed-first', first)
        later = self.save('合成局部压缩引用旧续接：方案甲仍待复核。', 'consumed-later', 'system')
        second = replace(first, expected_revision=1, anchor_id=later.message_id, progress=(later,), dependencies=(),
            consumed_checkpoints=(saved.checkpoint_id,))
        self.registry.save(self.host, 'consumed-later', second)
        confirmation = self.save('合成用户确认：不再召回旧工具结论。', 'consumed-confirm')
        self.runtime.change(self.owner, Change('suppress-consumed', tool.message_id, 1, 'suppress', confirmation.message_id, 'synthetic'))
        restored = self.registry.current(self.host, 'consumed', 'main')
        self.assertEqual(restored.state, 'source_unavailable')
        self.assertEqual(restored.progress, ())
        self.assertIn(tool.message_id, restored.omitted_sources)

    def test_runtime_recovery_cannot_resurrect_work_abandoned_after_the_snapshot(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.runtime_restore import publish_replayed_runtime
        with tempfile.TemporaryDirectory(prefix='evernook-continuation-restore-') as directory:
            root = Path(directory)
            config, access = initialize(root / 'source', client_id='restore-owner', entry='restore', allow_archive_review=True)
            runtime = MemoryRuntime(config)
            registry = Continuations(runtime)
            text = '合成任务：核对恢复后的任务状态。'
            receipt = runtime.submit(access, Event(Source('restore', 'work', 'main', 'goal'), 'user', 1, None, 'complete', text))
            goal = EvidenceSpan(receipt.message_id, 1, 'original', 1, 0, len(text), text)
            draft = ContinuationDraft('restore-task', 'main', 0, receipt.message_id, goal, (), (), 'in_progress',
                (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            registry.save(access, 'active', draft)
            runtime.export_archive(access, root / 'old')
            old = capture_control_checkpoint(runtime, access)
            registry.save(access, 'abandoned', replace(draft, expected_revision=1, state='abandoned'))
            current = capture_control_checkpoint(runtime, access)
            self.assertNotEqual(old.control_head, current.control_head)
            with self.assertRaisesRegex(MemoryFailure, 'archive_checkpoint_stale'):
                replay_archive_controls(runtime, access, root / 'old', root / 'stale', checkpoint=old)
            replay_archive_controls(runtime, access, root / 'old', root / 'old-review', checkpoint=current)
            repaired_config, repaired_access = publish_replayed_runtime(runtime, access, root / 'old-review', root / 'repaired-runtime', checkpoint=current,
                client_id='repaired', entry='restore')
            repaired = Continuations(MemoryRuntime(repaired_config)).current(repaired_access, 'restore-task', 'main')
            self.assertEqual(repaired.state, 'abandoned')
            self.assertEqual(repaired.goal, {})
            runtime.export_archive(access, root / 'current')
            replay_archive_controls(runtime, access, root / 'current', root / 'current-review', checkpoint=current)
            recovered_config, recovered_access = publish_replayed_runtime(runtime, access, root / 'current-review', root / 'recovered',
                checkpoint=current, client_id='recovered', entry='restore')
            recovered = Continuations(MemoryRuntime(recovered_config)).current(recovered_access, 'restore-task', 'main')
            self.assertEqual(recovered.state, 'abandoned')
            self.assertEqual(recovered.goal, {})

    def test_checkpoint_publication_requires_recall_before_any_foreign_source_validation(self) -> None:
        writer = pair_client(self.config, client_id='capture-only', entry='capture-only',
            access_path=self.config.parent / 'mcp-access-writer.json', scopes=('capture',))
        text = '合成自己的任务。'
        own = self.runtime.submit(writer, Event(Source('capture-only', 'work', 'main', 'goal'), 'user', 1, None, 'complete', text))
        own_span = EvidenceSpan(own.message_id, 1, 'original', 1, 0, len(text), text)
        other = self.save('合成另一入口的原文，不应允许无召回权限逐字猜测。', 'foreign-source')
        quote = replace(other, end=1, expected_text='合')
        draft = ContinuationDraft('scope', 'main', 0, own.message_id, own_span, (quote,), (), 'unknown',
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.registry.save(writer, 'scope', draft)
        with self.assertRaisesRegex(MemoryFailure, 'continuation_not_found'):
            self.registry.current(self.host, 'scope', 'main')
        reader = pair_client(self.config, client_id='recall-only', entry='recall-only',
            access_path=self.config.parent / 'mcp-access-reader.json', scopes=('recall',))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            self.registry.save(reader, 'scope', draft)


if __name__ == '__main__':
    unittest.main()
