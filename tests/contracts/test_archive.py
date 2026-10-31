from __future__ import annotations

from dataclasses import replace
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


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-archive-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)

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
        with patch("evernook.archive.time.monotonic", side_effect=[0.0, 31.0]):
            with self.assertRaisesRegex(MemoryFailure, "archive_snapshot_timeout"):
                self.runtime.export_archive(self.access, self.root / "export")
        self.assertFalse((self.root / "export").exists())
        self.assertEqual(self.runtime.status(self.access).messages, 0)
