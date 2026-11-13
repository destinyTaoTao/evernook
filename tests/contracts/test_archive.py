from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evernook import AttachmentInput, Capture, Change, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.archive import EvidenceArchive


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-archive-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)

    def replay_controls(self, source: Path, destination: Path) -> EvidenceArchive:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        return replay_archive_controls(self.runtime, self.access, source, destination, checkpoint=checkpoint)

    def test_export_restores_exact_evidence_and_important_words_for_review_without_runtime_secrets(self) -> None:
        from evernook import restore_archive

        event = Event(Source("test", "session", "main", "1"), "user", 1, None, "complete", "合成：37.50元。\r\n甲🙂e\u0301乙")
        receipt = self.runtime.submit(self.access, event)
        item = self.runtime.mark_important(self.access, ImportantMark(receipt.message_id, "original", 1, "shared", None, None, event.text))
        self.runtime.refresh_lexical(self.access)
        exported = self.runtime.export_archive(self.access, self.root / "export")
        self.assertEqual(exported.recovery_state, "review_required")
        archive = restore_archive(self.root / "export", self.root / "restored")
        self.assertEqual(archive.review_message(receipt.message_id).message.event, event)
        self.assertEqual(archive.review_important(item.important_id).page.text, event.text)
        self.assertTrue(archive.review_important(item.important_id).archive_only)
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertFalse(archive.status().backup_enabled)
        self.assertEqual(restore_archive(self.root / "export", self.root / "restored").status(), archive.status())
        contents = b"".join(path.read_bytes() for path in (self.root / "export").iterdir())
        self.assertNotIn(self.access.token.encode(), contents)
        self.assertNotIn((self.config.parent / "idempotency.key").read_bytes(), contents)
        with self.assertRaises(MemoryFailure):
            MemoryRuntime(self.root / "restored" / "runtime.json")

    def test_restore_onto_any_occupied_destination_reports_the_same_destination_code(self) -> None:
        from evernook import restore_archive

        event = Event(Source("test", "session", "main", "occupied"), "user", 1, None, "complete", "合成占用目标。")
        self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "export")
        occupied = self.root / "occupied"
        occupied.write_text("synthetic plain file, not an archive", encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            restore_archive(self.root / "export", occupied)
        # No overwrite happened.
        self.assertEqual(occupied.read_text(encoding="utf-8"), "synthetic plain file, not an archive")

    def test_tool_attachments_projection_and_suppression_survive_review_restore(self) -> None:
        from evernook import restore_archive

        event = Event(Source("test", "session", "branch", "1"), "user", 1, None, "complete", "合成普通输入")
        initial = self.runtime.submit(self.access, event)
        captured = self.runtime.submit_capture(self.access, Capture(replace(event, role="tool", source=replace(event.source, event="2")),
            "tool_result", initial.message_id, "synthetic-tool", (AttachmentInput("a1", "note.txt", "text/plain", (b"synthetic attachment",)),)))
        protected = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="3"), text="synthetic-protected-original", sensitive=True))
        self.runtime.change(self.access, Change("suppress-1", captured.message.message_id, 1, "suppress", initial.message_id, "synthetic instruction"))
        self.runtime.export_archive(self.access, self.root / "export")
        archive = restore_archive(self.root / "export", self.root / "restored")
        self.assertEqual(archive.review_message(captured.message.message_id).state.status, "suppressed")
        self.assertTrue(archive.review_message(captured.message.message_id).archive_only)
        capture_review = archive.review_capture(captured.message.message_id)
        assert capture_review is not None
        self.assertEqual(capture_review.receipt, captured)
        self.assertEqual(capture_review.state.status, "suppressed")
        self.assertEqual(archive.review_attachment(captured.message.message_id, "a1"), b"synthetic attachment")
        self.assertEqual(archive.review_message(protected.message_id).message.event.text, "[PROTECTED:ORIGINAL_NOT_SAVED]")
        self.assertNotIn(b"synthetic-protected-original", (self.root / "export" / "evidence.sqlite3").read_bytes())

    def test_old_snapshot_never_becomes_normal_recall_after_a_later_suppression(self) -> None:
        from evernook import restore_archive

        event = Event(Source("test", "s", "main", "1"), "user", 1, None, "complete", "historical synthetic evidence")
        receipt = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        evidence = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"), text="synthetic forget instruction"))
        self.runtime.change(self.access, Change("forget", receipt.message_id, 1, "suppress", evidence.message_id, "synthetic suppression"))
        archive = restore_archive(self.root / "old", self.root / "restored")
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertTrue(archive.review_message(receipt.message_id).archive_only)
        self.assertIsNone(self.runtime.read(self.access, receipt.message_id))
        self.assertFalse(hasattr(archive, "read"))
        self.assertFalse(hasattr(archive, "search"))
        self.assertEqual(archive.review_message_ids(), (receipt.message_id,))

    def test_replayed_old_snapshot_applies_later_suppression_and_stays_review_only(self) -> None:
        from evernook.archive import replay_archive_controls

        event = Event(Source("test", "replay", "main", "1"), "user", 1, None, "complete", "synthetic old evidence")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "replay-old")
        instruction = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"),
            text="synthetic suppression instruction"))
        self.runtime.change(self.access, Change("replay-suppress", target.message_id, 1, "suppress",
            instruction.message_id, "synthetic suppression"))
        archive = self.replay_controls(self.root / "replay-old", self.root / "replay-result")
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertEqual(archive.review_message(target.message_id).state.status, "suppressed")
        self.assertFalse(hasattr(archive, "read"))
        from evernook.archive import capture_control_checkpoint
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        self.assertEqual(archive.control_coverage().state, "unverified")
        self.assertEqual(archive.control_coverage(self.runtime, self.access).state, "unverified")
        self.assertEqual(archive.control_coverage(self.runtime, self.access, checkpoint=checkpoint).state, "current_at_check")
        self.assertEqual(self.replay_controls(self.root / "replay-old",
            self.root / "replay-result").status(), archive.status())
        instruction2 = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="3"),
            text="synthetic later revocation"))
        self.runtime.change(self.access, Change("replay-revoke", target.message_id, 2, "revoke",
            instruction2.message_id, "synthetic revocation"))
        self.assertEqual(archive.control_coverage(self.runtime, self.access, checkpoint=checkpoint).state, "stale")
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            self.replay_controls(self.root / "replay-old", self.root / "replay-result")

    def test_snapshot_ledger_identity_survives_reopen_without_exporting_the_key(self) -> None:
        self.runtime.export_archive(self.access, self.root / "ledger-bound")
        with closing(sqlite3.connect(self.root / "ledger-bound" / "evidence.sqlite3")) as db:
            first = db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()[0]
        reopened = MemoryRuntime(self.config)
        reopened.export_archive(self.access, self.root / "ledger-bound-again")
        with closing(sqlite3.connect(self.root / "ledger-bound-again" / "evidence.sqlite3")) as db:
            second = db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()[0]
        self.assertEqual(first, second)
        self.assertNotIn((self.config.parent / "idempotency.key").read_bytes(),
                         (self.root / "ledger-bound" / "evidence.sqlite3").read_bytes())

    def test_replay_rejects_a_foreign_ledger_and_applies_a_confirmed_purge(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.purge import PurgeDecision, PurgeWorkflow

        event = Event(Source("test", "purge-replay", "main", "1"), "user", 1, None, "complete", "synthetic delete target")
        target = self.runtime.submit(self.access, event)
        important = self.runtime.mark_important(self.access, ImportantMark(target.message_id, "original", 1,
            "synthetic", None, None, event.text))
        self.runtime.refresh_lexical(self.access)
        self.runtime.export_archive(self.access, self.root / "purge-old")
        foreign_config, foreign_access = initialize(self.root / "foreign", client_id="other", entry="other", allow_archive_review=True)
        with self.assertRaisesRegex(MemoryFailure, "archive_source_unverified"):
            replay_archive_controls(MemoryRuntime(foreign_config), foreign_access, self.root / "purge-old", self.root / "foreign-result", checkpoint=capture_control_checkpoint(MemoryRuntime(foreign_config), foreign_access))
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, "replay-purge", (target.message_id,))
        confirmation = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"),
            text="synthetic confirmed deletion"))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        archive = self.replay_controls(self.root / "purge-old", self.root / "purge-result")
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertNotIn(target.message_id, archive.review_message_ids())
        with self.assertRaisesRegex(MemoryFailure, "message_not_found"):
            archive.review_message(target.message_id)
        with self.assertRaisesRegex(MemoryFailure, "important_not_found"):
            archive.review_important(important.important_id)
        with closing(sqlite3.connect(archive.root / "evidence.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM purge_targets_v1 WHERE message_id=?", (target.message_id,)).fetchone())
        self.assertNotIn(b"synthetic delete target", (archive.root / "evidence.sqlite3").read_bytes())

    def test_replay_rejects_modified_snapshot_even_when_its_public_hash_is_recomputed(self) -> None:
        from evernook.archive import replay_archive_controls, verify_archive

        event = Event(Source("test", "tampered-replay", "main", "1"), "user", 1, None, "complete", "synthetic original")
        self.runtime.submit(self.access, event)
        source = self.root / "tampered-replay"
        self.runtime.export_archive(self.access, source)
        database = source / "evidence.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE messages SET event=json_set(event,'$.text','synthetic forged')")
            db.commit()
        manifest_path = source / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(verify_archive(source).recovery_state, "review_required")
        with self.assertRaisesRegex(MemoryFailure, "archive_source_unverified"):
            self.replay_controls(source, self.root / "tampered-result")
        self.assertFalse((self.root / "tampered-result").exists())

    def test_replay_requires_an_authenticated_unchanged_trusted_checkpoint(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls

        event = Event(Source("test", "checkpoint", "main", "1"), "user", 1, None, "complete", "synthetic checkpoint target")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "checkpoint-old")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        with self.assertRaisesRegex(MemoryFailure, "archive_checkpoint_required"):
            replay_archive_controls(self.runtime, self.access, self.root / "checkpoint-old", self.root / "no-checkpoint")
        forged = replace(checkpoint, control_head="0" * 64)
        with self.assertRaisesRegex(MemoryFailure, "archive_checkpoint_unverified"):
            replay_archive_controls(self.runtime, self.access, self.root / "checkpoint-old", self.root / "forged-checkpoint", checkpoint=forged)
        instruction = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"),
            text="synthetic later suppression"))
        self.runtime.change(self.access, Change("checkpoint-suppress", target.message_id, 1, "suppress",
            instruction.message_id, "synthetic"))
        with self.assertRaisesRegex(MemoryFailure, "archive_checkpoint_stale"):
            replay_archive_controls(self.runtime, self.access, self.root / "checkpoint-old", self.root / "stale-checkpoint", checkpoint=checkpoint)
        self.assertFalse((self.root / "stale-checkpoint").exists())

    def test_replay_rejects_diverged_immutable_message_even_with_fresh_checkpoint(self) -> None:
        from evernook.archive import capture_control_checkpoint, replay_archive_controls

        event = Event(Source("test", "message-divergence", "main", "1"), "user", 1, None, "complete", "synthetic original")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "message-old")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("UPDATE messages SET event=json_set(event,'$.text','synthetic modified') WHERE id=?", (target.message_id,))
            db.commit()
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        with self.assertRaisesRegex(MemoryFailure, "archive_control_incomplete"):
            replay_archive_controls(self.runtime, self.access, self.root / "message-old", self.root / "message-result", checkpoint=checkpoint)
        self.assertFalse((self.root / "message-result").exists())

    def test_replay_rejects_diverged_attachment_even_with_fresh_checkpoint(self) -> None:
        event = Event(Source("test", "attachment-divergence", "main", "1"), "user", 1, None, "complete", "synthetic capture")
        captured = self.runtime.submit_capture(self.access, Capture(event, "message", None, None,
            (AttachmentInput("a1", "note.txt", "text/plain", (b"synthetic original",)),)))
        self.runtime.export_archive(self.access, self.root / "attachment-old")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("UPDATE attachments SET content=? WHERE message_id=?", (b"synthetic modified", captured.message.message_id))
            db.commit()
        with self.assertRaisesRegex(MemoryFailure, "archive_control_incomplete"):
            self.replay_controls(self.root / "attachment-old", self.root / "attachment-result")
        self.assertFalse((self.root / "attachment-result").exists())

    def test_coverage_uses_newer_independently_retained_checkpoint_after_source_rollback(self) -> None:
        from evernook.archive import capture_control_checkpoint

        event = Event(Source("test", "rollback-watermark", "main", "1"), "user", 1, None, "complete", "synthetic target")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "rollback-old")
        archive = self.replay_controls(self.root / "rollback-old", self.root / "rollback-result")
        instruction = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"), text="synthetic suppress"))
        self.runtime.change(self.access, Change("rollback-suppress", target.message_id, 1, "suppress", instruction.message_id, "synthetic"))
        newer = capture_control_checkpoint(self.runtime, self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("DELETE FROM visibility WHERE message_id=?", (target.message_id,))
            db.execute("DELETE FROM changes WHERE target_id=?", (target.message_id,))
            db.execute("DELETE FROM jobs WHERE message_id=?", (instruction.message_id,))
            db.execute("DELETE FROM messages WHERE id=?", (instruction.message_id,))
            db.commit()
        self.assertEqual(archive.control_coverage(self.runtime, self.access).state, "unverified")
        self.assertEqual(archive.control_coverage(self.runtime, self.access, checkpoint=newer).state, "stale")

    def test_replay_rejects_divergent_fact_history_even_when_the_key_and_revision_match(self) -> None:
        import shutil
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.facts import FactDecision, FactRegistry

        text = "合成：冲突分支证据"
        receipt = self.runtime.submit(self.access, Event(Source("test", "fork", "main", "source"),
            "user", 1, None, "complete", text))
        candidates = CandidateWorkflow(self.runtime)
        lease = candidates.begin(self.access, (receipt.message_id,), processor_version="synthetic-fork")
        assert lease.token is not None
        span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)
        candidates.complete(self.access, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement", (span,)),))
        candidate = candidates.candidates(self.access, lease.status.run_id)[0]
        clone_config, _ = initialize(self.root / "forked", client_id="other", entry="other", allow_archive_review=True)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as source_db:
            with closing(sqlite3.connect(clone_config.parent / "memory.sqlite3")) as clone_db:
                source_db.backup(clone_db)
        for name in ("client.json", "idempotency.key"):
            shutil.copyfile(self.config.parent / name, clone_config.parent / name)
        clone = MemoryRuntime(clone_config)
        def decision(identifier: str) -> FactDecision:
            return FactDecision(identifier, "synthetic-person", "residence", candidate.candidate_id,
                receipt.message_id, 0, "assert", "2020-01-01T00:00:00+08:00")
        FactRegistry(self.runtime).decide(self.access, decision("branch-a"))
        FactRegistry(clone).decide(self.access, decision("branch-b"))
        self.runtime.export_archive(self.access, self.root / "fork-old")
        checkpoint = capture_control_checkpoint(clone, self.access)
        with self.assertRaisesRegex(MemoryFailure, "archive_control_incomplete"):
            replay_archive_controls(clone, self.access, self.root / "fork-old", self.root / "fork-result", checkpoint=checkpoint)
        self.assertFalse((self.root / "fork-result").exists())

    def test_replay_rejects_same_fact_id_and_revision_when_forked_payload_differs(self) -> None:
        import shutil
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan
        from evernook.archive import capture_control_checkpoint, replay_archive_controls
        from evernook.facts import FactDecision, FactRegistry

        workflow = CandidateWorkflow(self.runtime)
        evidence: list[tuple[str, str]] = []
        for index in range(2):
            text = f"合成：分叉来源{index}"
            receipt = self.runtime.submit(self.access, Event(Source("test", "payload-fork", "main", str(index)),
                "user", index + 1, None, "complete", text))
            lease = workflow.begin(self.access, (receipt.message_id,), processor_version="synthetic-payload-fork")
            assert lease.token is not None
            span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)
            workflow.complete(self.access, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement", (span,)),))
            evidence.append((workflow.candidates(self.access, lease.status.run_id)[0].candidate_id, receipt.message_id))
        clone_config, _ = initialize(self.root / "payload-forked", client_id="other", entry="other", allow_archive_review=True)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as source_db:
            with closing(sqlite3.connect(clone_config.parent / "memory.sqlite3")) as clone_db:
                source_db.backup(clone_db)
        for name in ("client.json", "idempotency.key"):
            shutil.copyfile(self.config.parent / name, clone_config.parent / name)
        clone = MemoryRuntime(clone_config)
        def decision(item: tuple[str, str]) -> FactDecision:
            return FactDecision("same-request-id", "synthetic-person", "residence", item[0], item[1], 0,
                "assert", "2020-01-01T00:00:00+08:00")
        FactRegistry(self.runtime).decide(self.access, decision(evidence[0]))
        FactRegistry(clone).decide(self.access, decision(evidence[1]))
        self.runtime.export_archive(self.access, self.root / "payload-fork-old")
        checkpoint = capture_control_checkpoint(clone, self.access)
        with self.assertRaisesRegex(MemoryFailure, "archive_control_incomplete"):
            replay_archive_controls(clone, self.access, self.root / "payload-fork-old", self.root / "payload-fork-result", checkpoint=checkpoint)

    def test_replay_applies_correction_and_revocation_with_original_control_evidence(self) -> None:
        from evernook.archive import replay_archive_controls

        one = Event(Source("test", "multiple-controls", "main", "1"), "user", 1, None, "complete", "synthetic old value")
        two = replace(one, source=replace(one.source, event="2"), sequence=2, text="synthetic revoked value")
        old = self.runtime.submit(self.access, one)
        revoked = self.runtime.submit(self.access, two)
        self.runtime.export_archive(self.access, self.root / "multi-old")
        correction_capture = self.runtime.submit_capture(self.access, Capture(
            replace(one, source=replace(one.source, event="3"), sequence=3, text="synthetic corrected value"),
            "message", None, None, (AttachmentInput("a1", "note.txt", "text/plain", (b"synthetic correction attachment",)),)))
        correction = correction_capture.message
        revocation = self.runtime.submit(self.access, replace(one, source=replace(one.source, event="4"),
            sequence=4, text="synthetic revoke instruction"))
        self.runtime.change(self.access, Change("replay-correct", old.message_id, 1, "correct", correction.message_id, "synthetic"))
        self.runtime.change(self.access, Change("replay-revocation", revoked.message_id, 1, "revoke", revocation.message_id, "synthetic"))
        archive = self.replay_controls(self.root / "multi-old", self.root / "multi-result")
        corrected = archive.review_message(old.message_id)
        self.assertEqual((corrected.state.status, corrected.state.replacement_id), ("corrected", correction.message_id))
        self.assertEqual(archive.review_message(correction.message_id).message.event.text, "synthetic corrected value")
        capture_review = archive.review_capture(correction.message_id)
        assert capture_review is not None
        self.assertEqual(capture_review.receipt, correction_capture)
        self.assertEqual(archive.review_attachment(correction.message_id, "a1"), b"synthetic correction attachment")
        self.assertEqual(archive.review_message(revoked.message_id).state.status, "revoked")
        self.assertTrue(corrected.archive_only)

    def test_replay_handles_purge_blocked_before_physical_cleanup(self) -> None:
        from evernook.archive import replay_archive_controls
        from evernook.purge import PurgeDecision, PurgeWorkflow

        event = Event(Source("test", "blocked-purge", "main", "1"), "user", 1, None, "complete", "synthetic blocked target")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "blocked-old")
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, "blocked-replay-purge", (target.message_id,))
        confirm = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"),
            text="synthetic confirmed deletion"))
        decision = PurgeDecision(preview.request_id, confirm.message_id)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("CREATE TRIGGER synthetic_replay_interrupt BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            workflow.purge(self.access, decision)
        archive = self.replay_controls(self.root / "blocked-old", self.root / "blocked-result")
        self.assertNotIn(target.message_id, archive.review_message_ids())
        from evernook.archive import capture_control_checkpoint
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        self.assertEqual(archive.control_coverage(self.runtime, self.access, checkpoint=checkpoint).state, "current_at_check")
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("DROP TRIGGER synthetic_replay_interrupt")
        workflow.purge(self.access, decision)
        self.assertEqual(archive.control_coverage(self.runtime, self.access, checkpoint=checkpoint).state, "stale")

    def test_replay_keeps_later_fact_correction_and_does_not_revive_old_value(self) -> None:
        from evernook import CandidateDraft, CandidateWorkflow, EvidenceSpan
        from evernook.archive import replay_archive_controls
        from evernook.facts import FactDecision, FactRegistry

        candidates = CandidateWorkflow(self.runtime)
        facts = FactRegistry(self.runtime)
        def decide(identifier: str, text: str, revision: int, kind: str, predecessor: str | None = None) -> None:
            receipt = self.runtime.submit(self.access, Event(Source("test", "facts-replay", "main", identifier),
                "user", revision + 1, None, "complete", text))
            lease = candidates.begin(self.access, (receipt.message_id,), processor_version="synthetic-replay")
            assert lease.token is not None
            span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)
            candidates.complete(self.access, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement", (span,)),))
            candidate = candidates.candidates(self.access, lease.status.run_id)[0]
            facts.decide(self.access, FactDecision(identifier, "synthetic-person", "residence", candidate.candidate_id,
                receipt.message_id, revision, kind, "2020-01-01T00:00:00+08:00", replaces_id=predecessor))
        decide("before-correction", "合成：旧错误住址", 0, "assert")
        self.runtime.export_archive(self.access, self.root / "fact-old")
        decide("after-correction", "合成：更正后的住址", 1, "correction", "before-correction")
        result = self.replay_controls(self.root / "fact-old", self.root / "fact-result")
        review = result.review_fact("synthetic-person", "residence")
        self.assertTrue(review.archive_only)
        view = review.view
        self.assertEqual(view.fact.text if view.fact else None, "合成：更正后的住址")
        self.assertEqual(result.status().recovery_state, "review_required")
        from evernook.purge import PurgeDecision, PurgeWorkflow
        current = facts.query(self.access, "synthetic-person", "residence")
        assert current.fact is not None
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, "purge-corrected-fact", (current.fact.sources[0].message_id,))
        confirmation = self.runtime.submit(self.access, Event(Source("test", "facts-replay", "main", "confirm-purge"),
            "user", 3, None, "complete", "合成：确认删除更正来源"))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        after_purge = self.replay_controls(self.root / "fact-old", self.root / "fact-purged")
        unavailable = after_purge.review_fact("synthetic-person", "residence").view
        self.assertIsNone(unavailable.fact)
        self.assertEqual(unavailable.state, "source_unavailable")
        with closing(sqlite3.connect(after_purge.root / "evidence.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM purge_targets_v1 WHERE message_id=?",
                (current.fact.sources[0].message_id,)).fetchone())

    def test_replay_applies_later_profile_suppression_in_review_copy(self) -> None:
        from evernook import EvidenceSpan
        from evernook.archive import replay_archive_controls
        from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry

        text = "合成：喜欢摄影"
        source = self.runtime.submit(self.access, Event(Source("test", "profile-replay", "main", "source"),
            "user", 1, None, "complete", text))
        registry = ProfileRegistry(self.runtime)
        registry.decide(self.access, ProfileDecision("replay-profile-propose", "synthetic-tag", 0, "propose",
            ProfileDraft("interest", "喜欢摄影", 0.8, (EvidenceSpan(source.message_id, 1, "original", 1, 0, len(text), text),))))
        confirm = self.runtime.submit(self.access, Event(Source("test", "profile-replay", "main", "confirm"),
            "user", 2, None, "complete", "合成：确认画像"))
        registry.decide(self.access, ProfileDecision("replay-profile-confirm", "synthetic-tag", 1, "confirm", confirmation_id=confirm.message_id))
        self.runtime.export_archive(self.access, self.root / "profile-old")
        suppress = self.runtime.submit(self.access, Event(Source("test", "profile-replay", "main", "suppress"),
            "user", 3, None, "complete", "合成：抑制画像"))
        registry.decide(self.access, ProfileDecision("replay-profile-suppress", "synthetic-tag", 2, "suppress", confirmation_id=suppress.message_id))
        archive = self.replay_controls(self.root / "profile-old", self.root / "profile-result")
        review = archive.review_profile("synthetic-tag")
        self.assertTrue(review.archive_only)
        self.assertEqual(review.view.state, "suppressed")

    def test_replay_preserves_later_experience_disable_in_review_copy(self) -> None:
        from evernook import EvidenceSpan
        from evernook.archive import replay_archive_controls
        from evernook.experience import ExperienceDecision, ExperienceDraft, ExperienceRegistry

        text = "合成：操作之前核对版本"
        source = self.runtime.submit(self.access, Event(Source("test", "experience-replay", "main", "source"),
            "user", 1, None, "complete", text))
        registry = ExperienceRegistry(self.runtime)
        draft = ExperienceDraft("lesson", "合成：核对版本", "合成条件", (EvidenceSpan(source.message_id, 1, "original", 1, 0, len(text), text),))
        registry.decide(self.access, ExperienceDecision("replay-experience-propose", "synthetic-lesson", 0, "propose", draft))
        confirm = self.runtime.submit(self.access, Event(Source("test", "experience-replay", "main", "confirm"),
            "user", 2, None, "complete", "合成：确认经验"))
        registry.decide(self.access, ExperienceDecision("replay-experience-enable", "synthetic-lesson", 1, "enable", confirmation_id=confirm.message_id))
        self.runtime.export_archive(self.access, self.root / "experience-old")
        disable = self.runtime.submit(self.access, Event(Source("test", "experience-replay", "main", "disable"),
            "user", 3, None, "complete", "合成：停用经验"))
        registry.decide(self.access, ExperienceDecision("replay-experience-disable", "synthetic-lesson", 2, "disable", confirmation_id=disable.message_id))
        archive = self.replay_controls(self.root / "experience-old", self.root / "experience-result")
        review = archive.review_experience("synthetic-lesson")
        self.assertTrue(review.archive_only)
        self.assertEqual(review.view.state, "disabled")

    def test_corrupt_file_or_attachment_and_unknown_format_never_publish_restored_archive(self) -> None:
        from evernook import restore_archive, verify_archive

        event = Event(Source("test", "s", "main", "1"), "user", 1, None, "complete", "synthetic evidence")
        self.runtime.submit_capture(self.access, Capture(event, "message", attachments=(AttachmentInput("a1", "a.txt", "text/plain", (b"synthetic",)),)))
        self.runtime.export_archive(self.access, self.root / "export")
        database = self.root / "export" / "evidence.sqlite3"
        original = database.read_bytes()
        database.write_bytes(original + b"corruption")
        with self.assertRaisesRegex(MemoryFailure, "archive_integrity_mismatch"):
            restore_archive(self.root / "export", self.root / "restored")
        self.assertFalse((self.root / "restored").exists())
        database.write_bytes(original)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE attachments SET content=?", (b"damaged",))
        db.close()
        manifest_path = self.root / "export" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "attachment_corrupt"):
            verify_archive(self.root / "export")
        with sqlite3.connect(database) as db:
            db.execute("UPDATE attachments SET content=randomblob(1048577)")
        db.close()
        manifest["status"]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "invalid_archive"):
            verify_archive(self.root / "export")
        manifest["version"] = 999
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "unsupported_archive"):
            restore_archive(self.root / "export", self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_failed_publication_has_no_successful_destination_and_retry_preserves_source(self) -> None:
        from evernook import restore_archive

        event = Event(Source("test", "s", "main", "1"), "user", 1, None, "complete", "synthetic evidence")
        receipt = self.runtime.submit(self.access, event)
        with patch("os.rename", side_effect=OSError("synthetic write failure")):
            with self.assertRaisesRegex(MemoryFailure, "archive_write_failed"):
                self.runtime.export_archive(self.access, self.root / "export")
        self.assertFalse((self.root / "export").exists())
        stored = self.runtime.read(self.access, receipt.message_id)
        assert stored is not None
        self.assertEqual(stored.event, event)
        self.runtime.export_archive(self.access, self.root / "export")
        with patch("os.rename", side_effect=OSError("synthetic write failure")):
            with self.assertRaisesRegex(MemoryFailure, "archive_write_failed"):
                restore_archive(self.root / "export", self.root / "restored")
        self.assertFalse((self.root / "restored").exists())
        self.assertEqual(restore_archive(self.root / "export", self.root / "restored").review_message(receipt.message_id).message.event, event)

    def test_process_exit_before_publication_leaves_no_complete_destination_and_can_retry(self) -> None:
        from evernook import restore_archive

        receipt = self.runtime.submit(self.access, Event(Source("test", "s", "main", "1"), "user", 1, None, "complete", "synthetic durable evidence"))
        worker = Path(__file__).with_name("archive_worker.py")
        for operation, source, destination in [("export", self.config, self.root / "export"),
                                                ("restore", self.root / "export", self.root / "restored")]:
            result = subprocess.run([sys.executable, str(worker), operation, str(source), str(destination)],
                                    capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 86, result.stderr.decode(errors="replace"))
            self.assertFalse(destination.exists())
            if operation == "export":
                self.runtime.export_archive(self.access, destination)
            else:
                archive = restore_archive(source, destination)
                self.assertEqual(archive.review_message(receipt.message_id).message.receipt, receipt)

    def test_export_requires_archive_grant_and_identity_and_refuses_existing_or_runtime_targets(self) -> None:
        peer_config, peer = initialize(self.root / "unprivileged", client_id="ordinary", entry="ordinary")
        operations = [(self.runtime, replace(self.access, token="wrong"), "unauthorized"),
                      (self.runtime, replace(self.access, contract_version=2), "incompatible_contract"),
                      (MemoryRuntime(peer_config), peer, "archive_forbidden")]
        for runtime, access, code in operations:
            with self.assertRaisesRegex(MemoryFailure, code):
                runtime.export_archive(access, self.root / "forbidden")
        self.assertFalse((self.root / "forbidden").exists())
        with self.assertRaisesRegex(MemoryFailure, "unsafe_archive_path"):
            self.runtime.export_archive(self.access, self.config.parent / "export")
        self.runtime.export_archive(self.access, self.root / "export")
        before = (self.root / "export" / "evidence.sqlite3").read_bytes()
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            self.runtime.export_archive(self.access, self.root / "export")
        self.assertEqual((self.root / "export" / "evidence.sqlite3").read_bytes(), before)

    def test_manifest_types_sidecars_and_conflicting_targets_are_rejected_without_overwrite(self) -> None:
        from evernook import restore_archive, verify_archive

        self.runtime.export_archive(self.access, self.root / "export")
        manifest_path = self.root / "export" / "manifest.json"
        original = manifest_path.read_text(encoding="utf-8")
        for field, value in [("messages", True), ("backup_enabled", True), ("recovery_state", "ready")]:
            manifest = json.loads(original)
            manifest["status"][field] = value
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(MemoryFailure, "invalid_archive"):
                verify_archive(self.root / "export")
        manifest_path.write_text(original, encoding="utf-8")
        sidecar = self.root / "export" / "evidence.sqlite3-wal"
        sidecar.write_bytes(b"synthetic active writer")
        with self.assertRaisesRegex(MemoryFailure, "archive_not_sealed"):
            restore_archive(self.root / "export", self.root / "restored")
        sidecar.unlink()
        self.runtime.export_archive(self.access, self.root / "other")
        before = (self.root / "other" / "manifest.json").read_bytes()
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            restore_archive(self.root / "export", self.root / "other")
        self.assertEqual((self.root / "other" / "manifest.json").read_bytes(), before)
        with self.assertRaisesRegex(MemoryFailure, "unsafe_archive_path"):
            restore_archive(self.root / "export", self.root / "export" / "nested")

    def test_snapshot_timeout_is_distinct_from_storage_busy_and_never_publishes_success(self) -> None:
        # Keep the fake clock local to archive code; runtime connection setup
        # also uses the shared time module and must not consume these two ticks.
        with patch("evernook.archive.time") as clock:
            clock.monotonic.side_effect = [0.0, 31.0]
            with self.assertRaisesRegex(MemoryFailure, "archive_snapshot_timeout"):
                self.runtime.export_archive(self.access, self.root / "export")
        self.assertFalse((self.root / "export").exists())
        self.assertEqual(self.runtime.status(self.access).messages, 0)
