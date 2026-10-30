from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nacl import pwhash
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox
from nacl.signing import VerifyKey

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.protected import ProtectedInbox, initialize_protected_inbox


class ProtectedTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-protected-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.access_path = self.config.parent / "mcp-access.json"
        pair_client(self.config, client_id=self.access.client_id, entry="synthetic", access_path=self.access_path)
        self.password = b"synthetic recovery password 123"
        self.inbox = initialize_protected_inbox(self.runtime, self.root / "inbox", self.root / "keys", self.password)
        self.event = Event(Source("synthetic", "session", "main", "1"), "user", 1, None,
                           "complete", "合成🙂\r\npassword=synthetic-protected-sentinel-123")

    def test_locked_receive_is_durable_linked_idempotent_and_original_absent_from_all_ordinary_paths(self) -> None:
        receipt = self.inbox.submit(self.access, self.event)
        self.assertEqual(receipt.state, "protected_linked")
        self.assertTrue(receipt.encrypted_original_saved)
        assert receipt.message is not None
        self.assertFalse(receipt.message.original_saved)
        original = self.runtime.read(self.access, receipt.message.message_id)
        assert original is not None
        self.assertEqual(original.event.text, "[PROTECTED:ENCRYPTED_ORIGINAL]")
        self.assertFalse(self.runtime.search(self.access, "synthetic-protected-sentinel-123").hits)
        self.runtime.rebuild_lexical(self.access)
        self.assertFalse(self.runtime.search(self.access, "synthetic-protected-sentinel-123").hits)
        reopened = ProtectedInbox(MemoryRuntime(self.config), self.root / "inbox", self.root / "keys")
        self.assertEqual(reopened.submit(self.access, self.event), receipt)
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        contents = b"".join(path.read_bytes() for path in self.root.rglob("*") if path.is_file())
        sentinel = b"synthetic-protected-sentinel-123"
        self.assertNotIn(sentinel, contents)
        self.assertNotIn(hashlib.sha256(sentinel).hexdigest().encode(), contents)
        # Test-only independent recovery proves ciphertext contains the exact original;
        # there is no plaintext-read operation on the inbox or ordinary runtime.
        wrapped = json.loads((self.root / "keys/recovery.json").read_text())
        key = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, self.password, bytes.fromhex(wrapped["salt"]),
            opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        private = json.loads(SecretBox(key).decrypt(bytes.fromhex(wrapped["ciphertext"])))
        with closing(sqlite3.connect(self.root / "inbox/inbox.sqlite3")) as db:
            envelope = json.loads(db.execute("SELECT envelope FROM objects").fetchone()[0])
        VerifyKey(bytes.fromhex(private["public"]["sender"])).verify(json.dumps(
            {"bound": envelope["bound"], "ciphertext": envelope["ciphertext"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            bytes.fromhex(envelope["signature"]))
        clear = json.loads(SealedBox(PrivateKey(bytes.fromhex(private["private"]))).decrypt(bytes.fromhex(envelope["ciphertext"])))
        self.assertEqual(clear["event"], asdict(self.event))
        self.assertEqual(clear["bound"], envelope["bound"])
        with self.assertRaises(CryptoError):
            SealedBox(PrivateKey.generate()).decrypt(bytes.fromhex(envelope["ciphertext"]))

    def test_competing_retries_and_source_conflict_preserve_single_cipher_object_and_projection(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.inbox.submit(self.access, self.event), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            self.inbox.submit(self.access, replace(self.event, text=self.event.text + "changed"))
        self.assertEqual(self.inbox.pending(self.access), ())

    def test_failed_ordinary_write_leaves_recoverable_ciphertext_without_false_linked_receipt(self) -> None:
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.inbox.submit(self.access, self.event)
        pending = self.inbox.pending(self.access)
        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].message)
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DROP TRIGGER synthetic_fail")
        linked = self.inbox.reconcile(self.access, pending[0].object_id)
        self.assertEqual(linked.state, "protected_linked")
        self.assertEqual(self.runtime.status(self.access).messages, 1)

    def test_actual_process_exit_before_projection_and_after_core_commit_recovers_without_decrypting(self) -> None:
        for phase, code, expected in (("before-link", 71, 0), ("after-core", 72, 2)):
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name("protected_worker.py")), str(self.config),
                str(self.access_path), str(self.root / "inbox"), str(self.root / "keys"), phase], capture_output=True, timeout=30)
            self.assertEqual(completed.returncode, code, completed.stderr.decode())
            self.assertEqual(self.runtime.status(self.access).messages, expected)
            pending = self.inbox.pending(self.access)
            self.assertEqual(len(pending), 1)
            receipt = self.inbox.reconcile(self.access, pending[0].object_id)
            self.assertEqual(receipt.state, "protected_linked")
            self.assertEqual(self.inbox.reconcile(self.access, receipt.object_id), receipt)
        self.assertEqual(self.runtime.status(self.access).messages, 2)

    def test_ciphertext_tamper_and_object_exchange_are_rejected_before_ordinary_link(self) -> None:
        one = self.inbox.stage(self.access, self.event)
        two = self.inbox.stage(self.access, replace(self.event, source=replace(self.event.source, event="2")))
        with closing(sqlite3.connect(self.root / "inbox/inbox.sqlite3", isolation_level=None)) as db:
            raw = db.execute("SELECT envelope FROM objects WHERE id=?", (two.object_id,)).fetchone()[0]
            db.execute("UPDATE objects SET envelope=? WHERE id=?", (raw, one.object_id))
        with self.assertRaisesRegex(MemoryFailure, "protected_object_corrupt"):
            self.inbox.reconcile(self.access, one.object_id)
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        with closing(sqlite3.connect(self.root / "inbox/inbox.sqlite3", isolation_level=None)) as db:
            value = json.loads(raw)
            value["ciphertext"] = "00" + value["ciphertext"][2:]
            db.execute("UPDATE objects SET envelope=? WHERE id=?", (json.dumps(value), two.object_id))
        with self.assertRaisesRegex(MemoryFailure, "protected_object_corrupt"):
            self.inbox.reconcile(self.access, two.object_id)
        self.assertEqual(self.runtime.status(self.access).messages, 0)

    def test_wrong_receiver_and_ledger_binding_and_foreign_entry_refuse_without_plaintext_fallback(self) -> None:
        other_config, other_access = initialize(self.root / "other", client_id="other", entry="other")
        other = initialize_protected_inbox(MemoryRuntime(other_config), self.root / "other-inbox", self.root / "other-keys", self.password)
        with self.assertRaisesRegex(MemoryFailure, "protected_binding_mismatch"):
            ProtectedInbox(self.runtime, self.root / "inbox", self.root / "other-keys")
        with self.assertRaisesRegex(MemoryFailure, "protected_binding_mismatch"):
            ProtectedInbox(MemoryRuntime(other_config), self.root / "inbox", self.root / "keys")
        with self.assertRaisesRegex(MemoryFailure, "unauthorized_source"):
            other.submit(other_access, self.event)
        self.assertEqual(self.runtime.status(self.access).messages, 0)

    def test_encryption_failure_and_missing_sender_material_never_fall_back_to_plaintext(self) -> None:
        with patch("evernook.protected.SealedBox.encrypt", side_effect=RuntimeError("synthetic secret diagnostic")):
            with self.assertRaisesRegex(MemoryFailure, "^protected_encryption_failed$"):
                self.inbox.submit(self.access, self.event)
        self.assertEqual(self.inbox.pending(self.access), ())
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        (self.root / "keys/writer.key").unlink()
        with self.assertRaises(MemoryFailure):
            self.inbox.submit(self.access, self.event)
        self.assertEqual(self.runtime.status(self.access).messages, 0)

    def test_linked_status_revalidates_ordinary_reference_and_reconcile_repairs_missing_link(self) -> None:
        receipt = self.inbox.submit(self.access, self.event)
        assert receipt.message is not None
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("DELETE FROM protected_links_v1 WHERE message_id=?", (receipt.message.message_id,))
        with self.assertRaisesRegex(MemoryFailure, "protected_link_unavailable"):
            self.inbox.status(self.access, receipt.object_id)
        self.assertEqual(self.inbox.reconcile(self.access, receipt.object_id), receipt)
        self.assertEqual(self.inbox.status(self.access, receipt.object_id), receipt)

    def test_explicit_cli_receive_and_reconcile_use_existing_inbox_and_never_print_secret(self) -> None:
        arguments = [sys.executable, "-m", "evernook.cli", "vault-receive", "--config", str(self.config),
            "--access", str(self.access_path), "--inbox-dir", str(self.root / "inbox"), "--key-dir", str(self.root / "keys")]
        result = subprocess.run(arguments, input=json.dumps(asdict(self.event)).encode(), capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        payload = json.loads(result.stdout)
        self.assertEqual(payload["result"]["state"], "protected_linked")
        self.assertNotIn(b"synthetic-protected-sentinel-123", result.stdout + result.stderr)
        arguments[3] = "vault-init"
        refused = subprocess.run(arguments, input=b"synthetic password", capture_output=True, timeout=30)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stdout)["error"]["code"], "interactive_vault_initialization_required")


if __name__ == "__main__":
    unittest.main()
