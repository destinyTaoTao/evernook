from __future__ import annotations

from collections.abc import Callable
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

    def test_ui_and_recovery_decisions_advance_the_control_checkpoint(self) -> None:
        from evernook.host_recovery import HostRecovery
        from evernook.management import MemoryManagement

        saved = self.runtime.submit_capture(self.access, Capture(Event(Source('test', 'review', 'main', '1'),
            'tool', 1, None, 'complete', '合成保存回执'), 'message'))
        management = MemoryManagement(self.runtime)
        recovery = HostRecovery(self.runtime)
        previous = capture_control_checkpoint(self.runtime, self.access)
        preview = management.preview(self.access, {'kind': 'source_suppress', 'message_id': saved.message.message_id})
        changes: tuple[Callable[[], object], ...] = (
            lambda: management.cancel(self.access, preview['proposal_id']),
            lambda: recovery.run(self.access, {'action': 'preview', 'fingerprint': 'a' * 64, 'references': []}),
        )
        for change in changes:
            change()
            current = capture_control_checkpoint(self.runtime, self.access)
            self.assertNotEqual(previous.control_head, current.control_head)
            previous = current
        proposal = recovery.run(self.access, {'action': 'preview', 'fingerprint': 'b' * 64, 'references': []})
        changes = (
            lambda: recovery.run(self.access, {'action': 'confirm', 'proposal_id': proposal['proposal_id'], 'fingerprint': 'b' * 64}),
            lambda: recovery.run(self.access, {'action': 'complete', 'operation_id': proposal['target_session']}),
        )
        for change in changes:
            previous = capture_control_checkpoint(self.runtime, self.access)
            change()
            self.assertNotEqual(previous.control_head, capture_control_checkpoint(self.runtime, self.access).control_head)

    def test_stale_ui_or_recovery_state_blocks_publication_and_fresh_snapshot_preserves_it(self) -> None:
        from evernook.host_recovery import HostRecovery
        from evernook.management import MemoryManagement
        from evernook.runtime_restore import publish_replayed_runtime

        saved = self.runtime.submit_capture(self.access, Capture(Event(Source('test', 'review', 'main', '1'),
            'tool', 1, None, 'complete', '合成保存回执'), 'message'))
        recovery = HostRecovery(self.runtime)
        management = MemoryManagement(self.runtime)
        for phase in ('management-cancel', 'management-confirm', 'recovery-cancel', 'recovery-confirm', 'recovery-complete'):
            with self.subTest(phase=phase):
                if phase.startswith('management-'):
                    proposal = management.preview(self.access, {'kind': 'source_suppress', 'message_id': saved.message.message_id})
                else:
                    proposal = recovery.run(self.access, {'action': 'preview', 'fingerprint': 'a' * 64, 'references': []})
                    if phase == 'recovery-complete':
                        recovery.run(self.access, {'action': 'confirm', 'proposal_id': proposal['proposal_id'], 'fingerprint': 'a' * 64})
                self.runtime.export_archive(self.access, self.root / (phase + '-old'))
                previous = capture_control_checkpoint(self.runtime, self.access)
                if phase == 'management-cancel':
                    management.cancel(self.access, proposal['proposal_id'])
                elif phase == 'management-confirm':
                    management.confirm(self.access, proposal['proposal_id'], {'kind': 'source_suppress', 'message_id': saved.message.message_id})
                elif phase == 'recovery-complete':
                    recovery.run(self.access, {'action': 'complete', 'operation_id': proposal['target_session']})
                else:
                    recovery.run(self.access, {'action': phase.removeprefix('recovery-'), 'proposal_id': proposal['proposal_id'],
                        **({'fingerprint': 'a' * 64} if phase == 'recovery-confirm' else {})})
                current = capture_control_checkpoint(self.runtime, self.access)
                self.assertNotEqual(previous.control_head, current.control_head)
                replay_archive_controls(self.runtime, self.access, self.root / (phase + '-old'),
                    self.root / (phase + '-review'), checkpoint=current)
                with self.assertRaisesRegex(MemoryFailure, 'archive_evidence_incomplete'):
                    publish_replayed_runtime(self.runtime, self.access, self.root / (phase + '-review'), self.root / (phase + '-bad'),
                        checkpoint=current, client_id='restored', entry='test')
                self.assertFalse((self.root / (phase + '-bad')).exists())
                self.runtime.export_archive(self.access, self.root / (phase + '-fresh'))
                replay_archive_controls(self.runtime, self.access, self.root / (phase + '-fresh'),
                    self.root / (phase + '-fresh-review'), checkpoint=current)
                config, owner = publish_replayed_runtime(self.runtime, self.access, self.root / (phase + '-fresh-review'),
                    self.root / (phase + '-good'), checkpoint=current, client_id='restored', entry='test')
                restored = MemoryRuntime(config)
                with self.runtime._connection() as live, restored._connection() as copy:
                    for table in ('management_previews_v1', 'host_recovery_previews_v1', 'host_recovery_operations_v1'):
                        if live.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                            self.assertEqual([tuple(row) for row in live.execute(f'SELECT * FROM {table} ORDER BY rowid')],
                                [tuple(row) for row in copy.execute(f'SELECT * FROM {table} ORDER BY rowid')])
                with self.assertRaises(MemoryFailure):
                    restored.handshake(self.access)
                if not phase.startswith('management-'):
                    with self.assertRaisesRegex(MemoryFailure, 'recovery_not_found'):
                        HostRecovery(restored).run(owner, {'action': 'confirm', 'proposal_id': proposal['proposal_id'], 'fingerprint': 'a' * 64})
                # Only an explicit new pairing may reuse the old actor's durable receipts.
                from evernook import pair_client
                paired = pair_client(config, client_id='source', entry='restored-maintenance',
                    access_path=config.parent / 'mcp-access-maintenance.json', scopes=('recall', 'maintain', 'accept'))
                if phase == 'management-cancel':
                    with self.assertRaisesRegex(MemoryFailure, 'management_preview_canceled'):
                        MemoryManagement(restored).confirm(paired, proposal['proposal_id'],
                            {'kind': 'source_suppress', 'message_id': saved.message.message_id})
                elif phase == 'management-confirm':
                    result = MemoryManagement(restored).confirm(paired, proposal['proposal_id'],
                        {'kind': 'source_suppress', 'message_id': saved.message.message_id})
                    self.assertEqual(result['state'], 'suppressed')
                elif phase == 'recovery-cancel':
                    with self.assertRaisesRegex(MemoryFailure, 'recovery_canceled'):
                        HostRecovery(restored).run(paired, {'action': 'confirm', 'proposal_id': proposal['proposal_id'], 'fingerprint': 'a' * 64})
                else:
                    result = HostRecovery(restored).run(paired,
                        {'action': 'confirm', 'proposal_id': proposal['proposal_id'], 'fingerprint': 'a' * 64})
                    self.assertEqual(result['state'], 'complete' if phase == 'recovery-complete' else 'confirmed')
                    self.assertEqual(result['target_session'], proposal['target_session'])

    def test_unused_optional_control_tables_preserve_legacy_checkpoint_identity(self) -> None:
        import sqlite3
        from evernook.archive import _control_head

        with sqlite3.connect(':memory:') as db:
            self.assertEqual(_control_head(db, b'synthetic-legacy-control-head-key'),
                '0153022613cc5617fd4c998dfb1e765f1a77a98a718a1c4c5803c112f8913150')

    def test_restore_requires_current_protected_inbox_registration(self) -> None:
        from evernook.protected import initialize_protected_inbox, known_inboxes
        from evernook.runtime_restore import publish_replayed_runtime

        self.runtime.export_archive(self.access, self.root / 'old')
        inbox = initialize_protected_inbox(self.runtime, self.root / 'inbox', self.root / 'keys',
                                          b'synthetic recovery password 123')
        inbox.stage(self.access, Event(Source('test', 'session', 'main', '1'), 'user', 1, None,
                                       'complete', 'synthetic private sentinel'))
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        with self.assertRaisesRegex(MemoryFailure, 'archive_control_incomplete'):
            replay_archive_controls(self.runtime, self.access, self.root / 'old', self.root / 'old-review',
                                    checkpoint=checkpoint)
        self.assertFalse((self.root / 'old-review').exists())

        self.runtime.export_archive(self.access, self.root / 'fresh')
        replay_archive_controls(self.runtime, self.access, self.root / 'fresh', self.root / 'fresh-review',
                                checkpoint=checkpoint)
        config, owner = publish_replayed_runtime(self.runtime, self.access, self.root / 'fresh-review',
                                                  self.root / 'restored', checkpoint=checkpoint,
                                                  client_id='restored', entry='test')
        self.assertEqual(known_inboxes(MemoryRuntime(config), owner), known_inboxes(self.runtime, self.access))

    def test_revoked_automatic_processor_cannot_return_through_an_older_snapshot(self) -> None:
        from evernook.automatic_preferences import PreferenceUpdates
        from evernook.runtime_restore import publish_replayed_runtime

        updates = PreferenceUpdates(self.runtime)
        processor = updates.settings(self.access).rules_processor
        updates.configure(self.access, enabled=True, admitted_processor=processor)
        self.runtime.export_archive(self.access, self.root / "admitted")
        updates.configure(self.access, enabled=True, admitted_processor=None)
        checkpoint = capture_control_checkpoint(self.runtime, self.access)
        replay_archive_controls(self.runtime, self.access, self.root / "admitted", self.root / "review", checkpoint=checkpoint)
        with self.assertRaisesRegex(MemoryFailure, "archive_evidence_incomplete"):
            publish_replayed_runtime(self.runtime, self.access, self.root / "review", self.root / "bad-runtime",
                checkpoint=checkpoint, client_id="restored", entry="test")
        self.assertFalse((self.root / "bad-runtime").exists())

        self.runtime.export_archive(self.access, self.root / "revoked")
        replay_archive_controls(self.runtime, self.access, self.root / "revoked", self.root / "current-review", checkpoint=checkpoint)
        config, owner = publish_replayed_runtime(self.runtime, self.access, self.root / "current-review", self.root / "good-runtime",
            checkpoint=checkpoint, client_id="restored", entry="test")
        restored = PreferenceUpdates(MemoryRuntime(config)).settings(owner)
        self.assertTrue(restored.enabled)
        self.assertIsNone(restored.admitted_processor)

    def test_automatic_admission_revocation_stales_the_previous_control_checkpoint(self) -> None:
        from evernook.automatic_preferences import PreferenceUpdates

        updates = PreferenceUpdates(self.runtime)
        processor = updates.settings(self.access).rules_processor
        updates.configure(self.access, enabled=True, admitted_processor=processor)
        previous = capture_control_checkpoint(self.runtime, self.access)
        updates.configure(self.access, enabled=True, admitted_processor=None)
        current = capture_control_checkpoint(self.runtime, self.access)
        self.assertNotEqual(current.control_head, previous.control_head)

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
