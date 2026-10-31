from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Access, Change, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.backup import create_backup, initialize_backup_keys, restore_backup, retain_backups
from evernook.paths import provision_private_directory
from nacl.signing import SigningKey


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-backup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(self.config)
        self.password = b"synthetic-recovery-password-123"
        self.keys = initialize_backup_keys(self.root / "keys", self.password)
        self.event = Event(Source("test", "session", "main", "original"), "user", 1, None,
                           "complete", "合成：加密备份原文37.50元。\r\n甲🙂e\u0301乙")

    def test_encrypted_snapshot_roundtrip_includes_important_original_but_no_plaintext_materials(self) -> None:
        receipt = self.runtime.submit(self.access, self.event)
        item = self.runtime.mark_important(self.access, ImportantMark(receipt.message_id, "original", 1, "shared", None, None, self.event.text))
        status = create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        self.assertFalse(status.schedule_enabled)
        files = b"".join(path.read_bytes() for path in (self.root / "backup").iterdir())
        self.assertNotIn(self.event.text.encode(), files)
        self.assertNotIn(self.access.token.encode(), files)
        self.assertNotIn((self.config.parent / "idempotency.key").read_bytes(), files)
        archive = restore_backup(self.root / "backup", self.root / "restored", Path(self.keys.recovery_file), self.password)
        self.assertEqual(archive.review_message(receipt.message_id).message.event, self.event)
        self.assertEqual(archive.review_important(item.important_id).page.text, self.event.text)
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertEqual({p.name for p in archive.root.iterdir()}, {"manifest.json", "evidence.sqlite3"})
        self.assertFalse((archive.root / "runtime.json").exists())

    def test_wrong_password_other_recipient_tamper_and_truncation_never_publish_a_restore(self) -> None:
        self.runtime.submit(self.access, self.event)
        create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        with self.assertRaisesRegex(MemoryFailure, "recovery_unlock_failed"):
            restore_backup(self.root / "backup", self.root / "wrong-password", Path(self.keys.recovery_file), b"synthetic-wrong-password-123")
        other = initialize_backup_keys(self.root / "other-keys", self.password)
        with self.assertRaises(MemoryFailure):
            restore_backup(self.root / "backup", self.root / "wrong-key", Path(other.recovery_file), self.password)
        encrypted = self.root / "backup" / "evidence.enc"
        original = encrypted.read_bytes()
        encrypted.write_bytes(original[:-1])
        with self.assertRaisesRegex(MemoryFailure, "backup_integrity_mismatch"):
            restore_backup(self.root / "backup", self.root / "truncated", Path(self.keys.recovery_file), self.password)
        encrypted.write_bytes(original)
        header = self.root / "backup" / "backup.json"
        value = json.loads(header.read_text(encoding="utf-8"))
        value["content"]["status"]["backup_id"] = "synthetic-forged-backup"
        header.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "backup_authentication_failed"):
            restore_backup(self.root / "backup", self.root / "forged", Path(self.keys.recovery_file), self.password)
        self.assertTrue(all(not (self.root / name).exists() for name in ("wrong-password", "wrong-key", "truncated", "forged")))

    def test_protected_projection_and_later_suppression_do_not_become_restored_recall(self) -> None:
        receipt = self.runtime.submit(self.access, self.event)
        protected = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="protected"),
            text="password=synthetic-backup-sentinel-123"))
        create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        evidence = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="suppress"), text="合成：不再召回第一条"))
        self.runtime.change(self.access, Change("synthetic-change", receipt.message_id, 1, "suppress", evidence.message_id, "synthetic"))
        archive = restore_backup(self.root / "backup", self.root / "restored", Path(self.keys.recovery_file), self.password)
        self.assertEqual(archive.status().recovery_state, "review_required")
        self.assertFalse(archive.review_message(protected.message_id).message.receipt.original_saved)
        self.assertNotIn(b"synthetic-backup-sentinel-123", (archive.root / "evidence.sqlite3").read_bytes())
        self.assertIsNone(self.runtime.read(self.access, receipt.message_id))

    def test_authentication_archive_permission_existing_targets_and_nested_paths_are_rejected(self) -> None:
        wrong = Access(self.access.client_id, "synthetic-wrong-token")
        with self.assertRaisesRegex(MemoryFailure, "unauthorized"):
            create_backup(self.runtime, wrong, self.root / "backup", self.root / "keys")
        for target in (self.config.parent / "nested", self.root / "keys" / "nested"):
            with self.assertRaisesRegex(MemoryFailure, "unsafe_backup_path"):
                create_backup(self.runtime, self.access, target, self.root / "keys")
        create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        with self.assertRaisesRegex(MemoryFailure, "backup_destination_exists"):
            create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        with self.assertRaisesRegex(MemoryFailure, "backup_keys_exist"):
            initialize_backup_keys(self.root / "keys", self.password)

    def test_stream_final_marker_and_order_are_required_even_with_a_resigned_outer_manifest(self) -> None:
        for number in range(3):
            self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event=str(number)),
                                                    text="synthetic-large-" + "x" * 700000))
        create_backup(self.runtime, self.access, self.root / "backup", self.root / "keys")
        encrypted = self.root / "backup" / "evidence.enc"
        header = self.root / "backup" / "backup.json"
        original = encrypted.read_bytes()
        frames = []
        offset = 0
        while offset < len(original):
            length = struct.unpack(">I", original[offset:offset + 4])[0]
            frames.append(original[offset:offset + 4 + length])
            offset += 4 + length
        self.assertGreaterEqual(len(frames), 4)
        signer = SigningKey((self.root / "keys" / "writer.key").read_bytes())
        for name, changed in [("missing-final", b"".join(frames[:-1])),
                              ("reordered", b"".join([frames[1], frames[0], *frames[2:]]))]:
            encrypted.write_bytes(changed)
            manifest = json.loads(header.read_text(encoding="utf-8"))
            manifest["content"]["files"]["evidence"]["sha256"] = hashlib.sha256(changed).hexdigest()
            canonical = json.dumps(manifest["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            manifest["signature"] = signer.sign(canonical).signature.hex()
            header.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(MemoryFailure):
                restore_backup(self.root / "backup", self.root / name, Path(self.keys.recovery_file), self.password)
            self.assertFalse((self.root / name).exists())

    def test_process_exit_before_publication_is_not_success_and_after_publication_restores(self) -> None:
        receipt = self.runtime.submit(self.access, self.event)
        worker = Path(__file__).with_name("backup_worker.py")
        for stage, code in [("before", 86), ("after", 87)]:
            target = self.root / ("backup-" + stage)
            process = subprocess.run([sys.executable, str(worker), str(self.config), str(self.root / "keys"),
                                      str(target), stage], capture_output=True, timeout=60)
            self.assertEqual(process.returncode, code, process.stderr)
            self.assertEqual(target.exists(), stage == "after")
        create_backup(self.runtime, self.access, self.root / "backup-before", self.root / "keys")
        for stage in ("before", "after"):
            archive = restore_backup(self.root / ("backup-" + stage), self.root / ("restore-" + stage),
                                     Path(self.keys.recovery_file), self.password)
            self.assertEqual(archive.review_message(receipt.message_id).message.event, self.event)

    def test_retention_preserves_hourly_daily_and_verified_anchor_and_never_removes_foreign_data(self) -> None:
        self.runtime.submit(self.access, self.event)
        backup_root = self.root / "backups"
        provision_private_directory(backup_root)
        now = datetime.now(timezone.utc)
        ids = []
        for number, hours in enumerate((0, 0.2, 1, 50, 51, 24 * 31)):
            with patch("evernook.backup.datetime") as clock:
                clock.now.return_value = now - timedelta(hours=hours)
                status = create_backup(self.runtime, self.access, backup_root / ("snapshot-" + str(number)), self.root / "keys")
            ids.append(status.backup_id)
        (backup_root / "foreign.txt").write_text("synthetic foreign data", encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "verified_recovery_anchor_required"):
            retain_backups(backup_root, self.root / "keys", recovery_verified_id="synthetic-missing", now=now)
        planned = retain_backups(backup_root, self.root / "keys", recovery_verified_id=ids[-1], now=now)
        self.assertEqual(set(planned.eligible_for_removal), {ids[1], ids[4]})
        self.assertEqual(planned.removed, ())
        self.assertIn(ids[-1], planned.kept)
        self.assertIn("foreign.txt", planned.skipped)
        applied = retain_backups(backup_root, self.root / "keys", recovery_verified_id=ids[-1], now=now, apply=True)
        self.assertEqual(set(applied.removed), {ids[1], ids[4]})
        self.assertTrue((backup_root / "foreign.txt").exists())
        self.assertTrue((backup_root / "snapshot-5").exists())

    def test_stuck_snapshot_is_skipped_and_reported_instead_of_aborting_retention(self) -> None:
        self.runtime.submit(self.access, self.event)
        backup_root = self.root / "backups-stuck"
        provision_private_directory(backup_root)
        now = datetime.now(timezone.utc)
        ids = []
        for number, hours in enumerate((0, 0.2, 1, 50, 51, 24 * 31)):
            with patch("evernook.backup.datetime") as clock:
                clock.now.return_value = now - timedelta(hours=hours)
                status = create_backup(self.runtime, self.access, backup_root / ("snapshot-" + str(number)), self.root / "keys")
            ids.append(status.backup_id)
        real_rmtree = shutil.rmtree

        def stuck(path: Path, *args: Any, **kwargs: Any) -> None:
            if path.name == "snapshot-4":
                raise OSError("synthetic stuck snapshot held by another process")
            real_rmtree(path, *args, **kwargs)

        with patch("evernook.backup.shutil.rmtree", stuck):
            applied = retain_backups(backup_root, self.root / "keys", recovery_verified_id=ids[-1], now=now, apply=True)
        # The stuck bundle is reported as skipped; the other removal and the full report survive.
        self.assertEqual(applied.removed, (ids[1],))
        self.assertIn("snapshot-4", applied.skipped)
        self.assertTrue((backup_root / "snapshot-4").exists())
        self.assertFalse((backup_root / "snapshot-1").exists())


if __name__ == "__main__":
    unittest.main()
