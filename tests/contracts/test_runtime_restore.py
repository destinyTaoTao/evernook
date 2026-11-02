from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from evernook import AttachmentInput, Capture, Change, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.archive import capture_control_checkpoint, replay_archive_controls


class RuntimeRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-runtime-restore-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "source", client_id="source", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)

    def test_stream_chunks_survive_verified_publication_and_newer_chunk_blocks_stale_snapshot(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime
        request = self.runtime.submit(self.access, Event(Source("test", "stream", "main", "input"), "user", 0, None, "complete", "合成请求"))
        event = Event(Source("test", "stream", "main", "answer"), "assistant", 1, None, "partial", "合成首块。")
        self.runtime.append_stream(self.access, event, index=0, carry="", input_id=request.message_id)
        self.runtime.export_archive(self.access, self.root / "old-stream")
        self.runtime.append_stream(self.access, replace(event, text="第二块。"), index=1, carry="", input_id=request.message_id)
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old-stream", self.root / "stale-review", checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, "archive_evidence_incomplete"):
            publish_replayed_runtime(self.runtime, self.access, self.root / "stale-review", self.root / "bad-runtime",
                checkpoint=checkpoint, client_id="bad", entry="test")
        self.runtime.export_archive(self.access, self.root / "current-stream")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "current-stream", self.root / "stream-review", checkpoint=checkpoint)
        config, access = publish_replayed_runtime(self.runtime, self.access, self.root / "stream-review", self.root / "stream-runtime",
            checkpoint=checkpoint, client_id="stream-recovered", entry="test")
        page = MemoryRuntime(config).read_stream(access, event.source)
        self.assertEqual(page.text, "合成首块。第二块。")
        self.assertEqual(page.next_index, 2)
        self.assertFalse(page.complete)

    def test_publish_replayed_runtime_preserves_evidence_controls_and_new_identity(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore", "main", "1"), "user", 1, None, "complete", "synthetic exact original")
        captured = self.runtime.submit_capture(self.access, Capture(event, "message", None, None,
            (AttachmentInput("a1", "note.txt", "text/plain", (b"synthetic attachment",)),)))
        important = self.runtime.mark_important(self.access, ImportantMark(captured.message.message_id,
            "original", 1, "shared", None, None, event.text))
        hidden = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"), text="synthetic hidden"))
        self.runtime.export_archive(self.access, self.root / "old")
        instruction = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="3"), text="synthetic suppress"))
        self.runtime.change(self.access, Change("restore-suppress", hidden.message_id, 1, "suppress", instruction.message_id, "synthetic"))
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        config, access = publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "recovered",
            checkpoint=checkpoint, client_id="recovered", entry="test")
        recovered = MemoryRuntime(config)
        self.assertNotEqual(access.token, self.access.token)
        self.assertEqual(recovered.status(access).pending_jobs, self.runtime.status(self.access).pending_jobs)
        original = recovered.read(access, captured.message.message_id)
        source_result = recovered.read_source(access, event.source)
        assert original is not None and source_result is not None
        self.assertEqual(original.event, event)
        self.assertEqual(source_result.receipt.message_id, captured.message.message_id)
        self.assertEqual(recovered.read_important(access, important.important_id).text, event.text)
        self.assertEqual(recovered.read_attachment(access, captured.message.message_id, "a1"), b"synthetic attachment")
        self.assertIsNone(recovered.read(access, hidden.message_id))
        self.assertFalse(any(hit.message_id == hidden.message_id for hit in recovered.search(access, "synthetic hidden", mode="exact").hits))
        self.assertEqual(recovered.submit_capture(access, Capture(event, "message", None, None,
            (AttachmentInput("a1", "note.txt", "text/plain", (b"synthetic attachment",)),))).message, captured.message)
        new = recovered.submit(access, replace(event, source=replace(event.source, event="4"), text="synthetic new"))
        new_result = recovered.read(access, new.message_id)
        assert new_result is not None
        self.assertEqual(new_result.event.text, "synthetic new")
        self.assertIsNone(self.runtime.read_source(self.access, replace(event.source, event="4")))
        shutil.copyfile(self.root / "old" / "evidence.sqlite3", config.parent / "memory.sqlite3")
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "recovered",
                checkpoint=checkpoint, client_id="recovered", entry="test")

    def test_unreplayed_or_stale_archive_and_occupied_target_are_rejected(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-gates", "main", "1"), "user", 1, None, "complete", "synthetic original")
        self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        with self.assertRaises(MemoryFailure):
            publish_replayed_runtime(self.runtime, self.access, self.root / "old", self.root / "unreplayed",
                checkpoint=checkpoint, client_id="new", entry="test")
        self.assertFalse((self.root / "unreplayed").exists())
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2")))
        with self.assertRaises(MemoryFailure):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "stale",
                checkpoint=checkpoint, client_id="new", entry="test")
        self.assertFalse((self.root / "stale").exists())
        (self.root / "occupied").mkdir()
        with self.assertRaises(MemoryFailure):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "occupied",
                checkpoint=checkpoint, client_id="new", entry="test")
        self.assertEqual(list((self.root / "occupied").iterdir()), [])

    def test_interrupted_publication_does_not_create_destination_and_can_retry(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-interrupted", "main", "1"), "user", 1, None, "complete", "synthetic")
        self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        args = (self.runtime, self.access, self.root / "review", self.root / "result")
        with patch("evernook.runtime_restore.os.rename", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(MemoryFailure, "archive_write_failed"):
                publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        self.assertFalse((self.root / "result").exists())
        config, access = publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        self.assertIsNotNone(MemoryRuntime(config).read_source(access, event.source))

    def test_publish_rejects_replay_missing_later_ordinary_evidence(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-missing", "main", "1"), "user", 1, None, "complete", "synthetic old")
        self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"), text="synthetic later"))
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, "archive_evidence_incomplete"):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "result",
                checkpoint=checkpoint, client_id="new", entry="test")
        self.assertFalse((self.root / "result").exists())

    def test_retry_after_publication_recovers_the_original_identity(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-published", "main", "1"), "user", 1, None, "complete", "synthetic")
        self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        rename = os.rename
        def interrupted(source: Path, destination: Path) -> None:
            rename(source, destination)
            raise OSError("synthetic interruption after publication")
        args = (self.runtime, self.access, self.root / "review", self.root / "result")
        with patch("evernook.runtime_restore.os.rename", side_effect=interrupted):
            with self.assertRaisesRegex(MemoryFailure, "archive_write_failed"):
                publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        first = publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        second = publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        self.assertEqual(first, second)
        self.assertIsNotNone(MemoryRuntime(first[0]).read_source(first[1], event.source))
        database = first[0].parent / "memory.sqlite3"
        database.unlink()
        with self.assertRaisesRegex(MemoryFailure, "archive_destination_exists"):
            publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        self.assertFalse(database.exists())

    def test_published_runtime_does_not_revive_deleted_source_or_accept_its_retry(self) -> None:
        from evernook.purge import PurgeDecision, PurgeWorkflow
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-purged", "main", "1"), "user", 1, None, "complete", "synthetic deleted")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.access, "restore-purge", (target.message_id,))
        confirmation = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2"), text="synthetic confirm"))
        workflow.purge(self.access, PurgeDecision(preview.request_id, confirmation.message_id))
        # A fresh snapshot also contains the unrelated confirmation evidence.
        self.runtime.export_archive(self.access, self.root / "current")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "current", self.root / "review", checkpoint=checkpoint)
        config, access = publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "result",
            checkpoint=checkpoint, client_id="new", entry="test")
        recovered = MemoryRuntime(config)
        self.assertIsNone(recovered.read(access, target.message_id))
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            recovered.submit(access, event)

    def test_retry_rejects_source_suppression_after_publication(self) -> None:
        from evernook.runtime_restore import publish_replayed_runtime

        event = Event(Source("test", "restore-stale-retry", "main", "1"), "user", 1, None, "complete", "synthetic")
        target = self.runtime.submit(self.access, event)
        self.runtime.export_archive(self.access, self.root / "old")
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        args = (self.runtime, self.access, self.root / "review", self.root / "result")
        publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")
        instruction = self.runtime.submit(self.access, replace(event, source=replace(event.source, event="2")))
        self.runtime.change(self.access, Change("late-suppress", target.message_id, 1, "suppress", instruction.message_id, "synthetic"))
        with self.assertRaisesRegex(MemoryFailure, "archive_checkpoint_stale"):
            publish_replayed_runtime(*args, checkpoint=checkpoint, client_id="new", entry="test")

    def test_changed_navigation_pause_prevents_publishing_old_maintenance_state(self) -> None:
        from evernook.maintenance import NavigationMaintenance, NavigationPolicy
        from evernook.runtime_restore import publish_replayed_runtime

        maintenance = NavigationMaintenance(self.runtime)
        policy = NavigationPolicy(0)
        maintenance.configure(self.access, policy)
        self.runtime.export_archive(self.access, self.root / "old")
        maintenance.pause(self.access, policy.policy_id, paused=True)
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "old", self.root / "review", checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, "archive_evidence_incomplete"):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "result",
                checkpoint=checkpoint, client_id="new", entry="test")
