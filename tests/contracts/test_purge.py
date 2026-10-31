from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from evernook import Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.purge import PurgeDecision, PurgeWorkflow


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-purge-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test",
                                               allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.workflow = PurgeWorkflow(self.runtime)
        self.sequence = 0

    def save(self, text: str, *, role: str = "user") -> str:
        self.sequence += 1
        return self.runtime.submit(self.access, Event(Source("test", "purge-session", "main", str(self.sequence)),
            role, self.sequence, None, "complete", text)).message_id

    def event(self, event_id: str, text: str) -> Event:
        return Event(Source("test", "purge-session", "main", event_id), "user", 1, None, "complete", text)

    def test_confirmed_purge_removes_original_and_important_evidence_without_source_retry_resurrection(self) -> None:
        original = self.event("doomed", "合成彻底删除目标：37.50元。")
        target = self.runtime.submit(self.access, original).message_id
        important = self.runtime.mark_important(self.access, ImportantMark(target, "original", 1, "synthetic", None, None,
            "合成彻底删除目标：37.50元。"))
        preview = self.workflow.preview(self.access, "purge-original", (target,))
        confirmation = self.save("合成用户明确确认彻底删除刚才预览的唯一来源。")
        decision = PurgeDecision(preview.request_id, confirmation)

        result = self.workflow.purge(self.access, decision)

        self.assertEqual((result.state, result.source_ids), ("completed", (target,)))
        self.assertEqual(self.workflow.purge(self.access, decision), result)
        self.assertIsNone(self.runtime.read(self.access, target))
        self.assertEqual(self.runtime.search(self.access, "37.50元", mode="exact").hits, ())
        with self.assertRaisesRegex(MemoryFailure, "important_not_found"):
            self.runtime.read_important(self.access, important.important_id)
        with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
            self.runtime.review_archive(self.access, target)
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.submit(self.access, original)
        self.assertIsNotNone(self.runtime.read(self.access, confirmation))

    def test_mixed_derived_records_and_old_worker_results_cannot_restore_purged_evidence(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, DerivedContext, EvidenceSpan
        from evernook.experience import ExperienceDecision, ExperienceDraft, ExperienceRegistry
        from evernook.facts import FactDecision, FactRegistry

        target_text = "合成被删事实：小林住杭州。"
        keeper_text = "合成保留事实：小林喜欢乌龙茶。"
        target = self.save(target_text)
        keeper = self.save(keeper_text)
        stored_target = self.runtime.read(self.access, target)
        stored_keeper = self.runtime.read(self.access, keeper)
        assert stored_target is not None and stored_keeper is not None
        spans = tuple(EvidenceSpan(message.receipt.message_id, 1, "original", 1, 0, len(message.event.text), message.event.text)
                      for message in (stored_target, stored_keeper))
        candidates = CandidateWorkflow(self.runtime)
        lease = candidates.begin(self.access, (target, keeper), processor_version="synthetic-purge-v1")
        assert lease.token is not None
        draft = CandidateDraft("合成混合事实", "user_statement", spans)
        candidates.complete(self.access, lease.status.run_id, lease.token, (draft,))
        candidate_id = candidates.candidates(self.access, lease.status.run_id)[0].candidate_id
        confirmation = self.save("合成用户确认这条混合事实。")
        facts = FactRegistry(self.runtime)
        facts.decide(self.access, FactDecision("fact-before-purge", "synthetic-person", "residence", candidate_id,
            confirmation, 0, "assert", "2020-01-01T00:00:00Z"))
        experiences = ExperienceRegistry(self.runtime)
        experiences.decide(self.access, ExperienceDecision("experience-before-purge", "synthetic-lesson", 0, "propose",
            ExperienceDraft("lesson", "合成混合经验", "仅用于合成测试", spans)))
        context = DerivedContext("context-before-purge", target, 1, "合成旧摘要：杭州。")
        self.runtime.publish_context(self.access, context)
        self.runtime.refresh_lexical(self.access)
        running = candidates.begin(self.access, (target,), processor_version="synthetic-old-worker-v1")
        assert running.token is not None

        preview = self.workflow.preview(self.access, "purge-derived", (target,))
        approval = self.save("合成用户明确确认彻底删除派生测试中的目标来源。")
        self.workflow.purge(self.access, PurgeDecision(preview.request_id, approval))

        with self.assertRaisesRegex(MemoryFailure, "candidate_run_not_found"):
            candidates.status(self.access, lease.status.run_id)
        with self.assertRaisesRegex(MemoryFailure, "candidate_run_not_found"):
            candidates.complete(self.access, running.status.run_id, running.token, ())
        self.assertEqual(facts.query(self.access, "synthetic-person", "residence").state, "no_fact")
        with self.assertRaisesRegex(MemoryFailure, "experience_not_found"):
            experiences.read(self.access, "synthetic-lesson")
        self.assertIsNone(self.runtime.read_context(self.access, context.result_id))
        with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
            self.runtime.publish_context(self.access, context)
        self.assertEqual(self.runtime.search(self.access, "杭州", mode="exact").hits, ())
        self.assertEqual(self.runtime.read(self.access, keeper), stored_keeper)

    def test_preview_is_not_deletion_and_requires_later_user_confirmation_with_unchanged_scope(self) -> None:
        from evernook import Change

        original = self.event("preview-target", "合成预览时尚未删除。")
        receipt = self.runtime.submit(self.access, original)
        old_confirmation = self.save("合成预览前的旧确认。")
        preview = self.workflow.preview(self.access, "purge-preview", (receipt.message_id,))
        self.assertEqual(self.runtime.submit(self.access, original), receipt)
        with self.assertRaisesRegex(MemoryFailure, "purge_user_confirmation_required"):
            self.workflow.purge(self.access, PurgeDecision(preview.request_id, old_confirmation))
        assistant = self.save("合成助手不能代替用户确认彻底删除。", role="assistant")
        with self.assertRaisesRegex(MemoryFailure, "purge_user_confirmation_required"):
            self.workflow.purge(self.access, PurgeDecision(preview.request_id, assistant))
        suppression = self.save("合成用户先抑制预览目标。")
        self.runtime.change(self.access, Change("suppress-before-purge", receipt.message_id, 1, "suppress", suppression,
            "synthetic scope change"))
        with self.assertRaisesRegex(MemoryFailure, "purge_scope_changed"):
            self.workflow.purge(self.access, PurgeDecision(preview.request_id, suppression))

        replacement = self.workflow.preview(self.access, "purge-after-change", (receipt.message_id,))
        self.assertEqual(replacement.expected_versions, (2,))
        confirmation = self.save("合成用户确认删除已重新预览的抑制来源。")
        result = self.workflow.purge(self.access, PurgeDecision(replacement.request_id, confirmation))
        self.assertEqual(result.state, "completed")
        self.assertIsNone(self.runtime.read(self.access, receipt.message_id))

    def test_interrupted_cleanup_stays_blocked_and_same_confirmation_recovers(self) -> None:
        from contextlib import closing
        import sqlite3

        target = self.save("合成中断清理目标。")
        preview = self.workflow.preview(self.access, "purge-crash", (target,))
        confirmation = self.save("合成用户确认彻底删除中断测试目标。")
        decision = PurgeDecision(preview.request_id, confirmation)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("""CREATE TRIGGER synthetic_purge_interruption BEFORE DELETE ON messages
                BEGIN SELECT RAISE(ABORT,'synthetic purge interruption'); END""")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.workflow.purge(self.access, decision)
        reopened = MemoryRuntime(self.config)
        self.assertIsNone(reopened.read(self.access, target))
        self.assertEqual(reopened.state(self.access, target).status, "purged")
        from evernook import Change
        instruction = self.save("合成用户随后抑制早前的删除确认。")
        reopened.change(self.access, Change("suppress-purge-confirmation", confirmation, 1, "suppress", instruction,
            "synthetic later confirmation change"))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("DROP TRIGGER synthetic_purge_interruption")

        result = PurgeWorkflow(reopened).purge(self.access, decision)

        self.assertEqual(result.state, "completed")
        self.assertIsNone(reopened.read(self.access, target))
        self.assertEqual(PurgeWorkflow(MemoryRuntime(self.config)).purge(self.access, decision), result)

    def test_interrupted_cleanup_rejects_new_capture_dependencies_and_recovers(self) -> None:
        from contextlib import closing
        import sqlite3
        from evernook import Capture

        target = self.save("合成中断后不能新增引用的删除目标。")
        preview = self.workflow.preview(self.access, "purge-capture-race", (target,))
        confirmation = self.save("合成用户确认彻底删除中断后的目标。")
        decision = PurgeDecision(preview.request_id, confirmation)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("""CREATE TRIGGER synthetic_purge_capture_interruption BEFORE DELETE ON messages
                BEGIN SELECT RAISE(ABORT,'synthetic purge interruption'); END""")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.workflow.purge(self.access, decision)

        dependent = Event(Source("test", "purge-session", "main", "post-block-dependent"), "tool", 9, None,
            "complete", "合成清理中断后的工具结果。")
        with self.assertRaisesRegex(MemoryFailure, "not_recallable"):
            self.runtime.submit_capture(self.access, Capture(dependent, "tool_result", input_id=target,
                tool_name="synthetic-tool"))
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("DROP TRIGGER synthetic_purge_capture_interruption")

        result = PurgeWorkflow(MemoryRuntime(self.config)).purge(self.access, decision)

        self.assertEqual(result.state, "completed")
        self.assertIsNone(self.runtime.read(self.access, target))

    def test_scope_includes_capture_references_and_preserves_unshared_attachment_content(self) -> None:
        from evernook import AttachmentInput, Capture

        target = self.save("合成工具输入。")
        attachment = AttachmentInput("shared-content", "synthetic.txt", "text/plain", (b"same synthetic bytes",))
        tool_event = Event(Source("test", "purge-session", "main", "tool-result"), "tool", 2, None, "complete",
            "合成工具结果。")
        tool = self.runtime.submit_capture(self.access, Capture(tool_event, "tool_result", input_id=target,
            tool_name="synthetic-tool", attachments=(attachment,))).message.message_id
        keeper_event = Event(Source("test", "purge-session", "main", "keeper-capture"), "tool", 3, None, "complete",
            "合成范围外工具结果。")
        keeper = self.runtime.submit_capture(self.access, Capture(keeper_event, "tool_result", input_id=self.save("合成另一输入。"),
            tool_name="synthetic-tool", attachments=(attachment,))).message.message_id
        with self.assertRaisesRegex(MemoryFailure, "purge_scope_has_dependents"):
            self.workflow.preview(self.access, "purge-incomplete-capture-scope", (target,))

        preview = self.workflow.preview(self.access, "purge-complete-capture-scope", (target, tool))
        confirmation = self.save("合成用户确认彻底删除工具输入、结果及其附件。")
        self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))

        self.assertIsNone(self.runtime.read_capture(self.access, tool))
        self.assertIsNone(self.runtime.read_attachment(self.access, tool, attachment.attachment_id))
        self.assertEqual(self.runtime.read_attachment(self.access, keeper, attachment.attachment_id), b"same synthetic bytes")

    def test_mixed_navigation_summary_is_invalidated_and_rebuilt_only_from_remaining_sources(self) -> None:
        from evernook.maintenance import MaintenanceBudget, NavigationMaintenance, NavigationPolicy

        target = self.save("合成混合摘要中的删除来源。")
        keeper = self.save("合成混合摘要中的保留来源。")
        navigation = NavigationMaintenance(self.runtime)
        policy = navigation.configure(self.access, NavigationPolicy(0)).policy_id
        for _ in range(4):
            tick = navigation.tick(self.access, policy, budget=MaintenanceBudget(64, 16, 5))
            if not tick.status.queued:
                break
        bucket = next(item for item in tick.status.buckets if item.startswith("day:"))
        before = navigation.read(self.access, policy, bucket)
        assert before.summary is not None
        self.assertEqual({quote.source.message_id for quote in before.summary.quotes}, {target, keeper})

        preview = self.workflow.preview(self.access, "purge-navigation", (target,))
        confirmation = self.save("合成用户确认彻底删除导航摘要来源。")
        self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        invalidated = navigation.read(self.access, policy, bucket)
        self.assertIsNone(invalidated.summary)
        for _ in range(4):
            tick = navigation.tick(self.access, policy, budget=MaintenanceBudget(64, 16, 5))
            if not tick.status.queued:
                break
        rebuilt = navigation.read(self.access, policy, bucket)
        assert rebuilt.summary is not None
        rebuilt_sources = {quote.source.message_id for quote in rebuilt.summary.quotes}
        self.assertNotIn(target, rebuilt_sources)
        self.assertIn(keeper, rebuilt_sources)

    def test_semantic_recall_cannot_return_deleted_chunks_after_reopen(self) -> None:
        from threading import Event as ThreadEvent
        from evernook import EmbeddingSpace, SemanticRecall

        class Backend:
            space = EmbeddingSpace("synthetic-purge", "v1", 3, "synthetic-digest", 64, 16)

            def embed(self, texts: tuple[str, ...], *, timeout: float,
                      cancel: ThreadEvent | None = None) -> tuple[tuple[float, ...], ...]:
                return tuple((1.0, 0.0, 0.0) for _ in texts)

        target = self.save("合成语义删除目标：喜欢辛辣。")
        keeper = self.save("合成语义保留来源：喜欢清淡。")
        semantic = SemanticRecall(self.runtime, Backend())
        semantic.refresh(self.access)
        self.assertIn(target, {hit.evidence.message_id for hit in semantic.recall(self.access, "辛辣").hits})
        preview = self.workflow.preview(self.access, "purge-semantic", (target,))
        confirmation = self.save("合成用户确认彻底删除语义索引来源。")
        self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))

        reopened = SemanticRecall(MemoryRuntime(self.config), Backend())
        reopened.refresh(self.access)
        self.assertNotIn(target, {hit.evidence.message_id for hit in reopened.recall(self.access, "辛辣").hits})
        self.assertIsNotNone(self.runtime.read(self.access, keeper))

    def test_existing_archive_remains_review_only_and_protected_projection_is_not_plaintext_purged(self) -> None:
        from dataclasses import replace
        from evernook import restore_archive

        target = self.save("合成旧归档中的删除目标。")
        root = self.config.parent.parent
        self.runtime.export_archive(self.access, root / "old-archive")
        preview = self.workflow.preview(self.access, "purge-archive", (target,))
        confirmation = self.save("合成用户确认彻底删除当前账本目标，保留旧归档残留报告。")
        result = self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        self.assertEqual(result.residuals, ("existing_archives_and_backups_retained", "host_copies_not_controlled"))
        archive = restore_archive(root / "old-archive", root / "old-restored")
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertEqual(archive.review_message(target).message.event.text, "合成旧归档中的删除目标。")

        protected_event = replace(self.event("protected", "合成受保护原件。"), sensitive=True)
        protected = self.runtime.submit(self.access, protected_event).message_id
        with self.assertRaisesRegex(MemoryFailure, "protected_purge_unsupported"):
            self.workflow.preview(self.access, "purge-protected", (protected,))
        self.assertIsNotNone(self.runtime.read(self.access, protected))

    def test_new_out_of_scope_reference_after_preview_is_rejected_before_blocking(self) -> None:
        from evernook import Capture

        target = self.save("合成预览后新增引用的目标。")
        preview = self.workflow.preview(self.access, "purge-racing-reference", (target,))
        dependent = Event(Source("test", "purge-session", "main", "late-dependent"), "tool", 9, None, "complete",
            "合成预览后的工具结果。")
        self.runtime.submit_capture(self.access, Capture(dependent, "tool_result", input_id=target,
            tool_name="synthetic-tool"))
        confirmation = self.save("合成用户确认删除，但范围已出现新依赖。")
        with self.assertRaisesRegex(MemoryFailure, "purge_scope_has_dependents"):
            self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        self.assertIsNotNone(self.runtime.read(self.access, target))

    def test_trusted_cli_previews_then_purges_without_an_mcp_write_tool(self) -> None:
        import json
        import subprocess
        import sys
        from typing import cast
        from evernook import pair_client

        access_path = self.config.parent / "mcp-access-purge.json"
        pair_client(self.config, client_id="synthetic", entry="test", access_path=access_path)
        target = self.save("合成 CLI 删除目标。")

        def call(command: str, payload: dict[str, object]) -> dict[str, object]:
            child = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                "--access", str(access_path)], input=json.dumps(payload).encode(), capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout)
            result = json.loads(child.stdout)
            self.assertIsInstance(result, dict)
            self.assertTrue(result["ok"])
            value = result["result"]
            self.assertIsInstance(value, dict)
            return cast(dict[str, object], value)

        preview = call("purge-preview", {"request_id": "cli-purge", "source_ids": [target]})
        confirmation = self.save("合成用户通过可信 CLI 确认彻底删除预览来源。")
        result = call("purge", {"request_id": "cli-purge", "confirmation_id": confirmation})
        self.assertEqual(preview["source_ids"], [target])
        self.assertEqual(result["state"], "completed")
        self.assertIsNone(self.runtime.read(self.access, target))

    def test_pre_purge_encrypted_backup_restores_only_for_review_with_reported_residual(self) -> None:
        from evernook.backup import create_backup, initialize_backup_keys, restore_backup

        root = self.config.parent.parent
        password = b"synthetic-purge-recovery-password"
        keys = initialize_backup_keys(root / "backup-keys", password)
        target = self.save("合成加密备份中的删除目标。")
        create_backup(self.runtime, self.access, root / "pre-purge-backup", root / "backup-keys")
        preview = self.workflow.preview(self.access, "purge-encrypted-backup", (target,))
        confirmation = self.save("合成用户确认删除当前账本并保留旧加密备份残留。")
        result = self.workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation))
        self.assertIn("existing_archives_and_backups_retained", result.residuals)

        archive = restore_backup(root / "pre-purge-backup", root / "pre-purge-restored", Path(keys.recovery_file), password)
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertEqual(archive.review_message(target).message.event.text, "合成加密备份中的删除目标。")
        self.assertIsNone(self.runtime.read(self.access, target))


if __name__ == "__main__":
    unittest.main()
