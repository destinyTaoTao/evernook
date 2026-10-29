"""Synthetic-only behavior checks at the candidate encryption/decryption boundary."""
from __future__ import annotations

from dataclasses import replace
import secrets
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import sys
import os
from contextlib import contextmanager
from collections.abc import Iterator
import unittest

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey

from vault_probe import Authority, BoundObject, Inbox, Request, decrypt, seal, unwrap_key, wrap_key


@contextmanager
def evidence_root(prefix: str) -> Iterator[str]:
    location = os.environ.get("EVERNOOK_VAULT_RUN_ROOT")
    if location:
        root = tempfile.mkdtemp(prefix=prefix, dir=location)
        yield root  # Preserve only encrypted objects/public data in evidence runs.
    else:
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            yield temporary


class CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipient = PrivateKey.generate()
        self.sender = SigningKey.generate()
        self.bound = BoundObject("synthetic-object", 1, "synthetic-source", "key-1")
        self.value = ("SYNTHETIC_ONLY_" + secrets.token_hex(24)).encode()

    def test_public_key_only_writer_cannot_decrypt_and_receiver_restores_exact_bytes(self) -> None:
        envelope = seal(self.recipient.public_key, self.sender, self.bound, self.value)
        with self.assertRaises(TypeError):
            SealedBox(self.recipient.public_key).decrypt(envelope.ciphertext)  # type: ignore[misc]  # Intentional runtime misuse must fail.
        self.assertEqual(decrypt(self.recipient, self.sender.verify_key, self.bound, envelope), self.value)

    def test_wrong_key_tamper_and_object_or_source_swap_are_rejected(self) -> None:
        envelope = seal(self.recipient.public_key, self.sender, self.bound, self.value)
        with self.assertRaises(CryptoError):
            decrypt(PrivateKey.generate(), self.sender.verify_key, self.bound, envelope)
        altered = bytes([envelope.ciphertext[0] ^ 1]) + envelope.ciphertext[1:]
        with self.assertRaises(CryptoError):
            decrypt(self.recipient, self.sender.verify_key, self.bound, replace(envelope, ciphertext=altered))
        for bound in [replace(self.bound, source="other"), replace(self.bound, object_id="other"), replace(self.bound, version=2), replace(self.bound, key_id="key-2")]:
            with self.assertRaises(ValueError):
                decrypt(self.recipient, self.sender.verify_key, bound, envelope)
        with self.assertRaises(CryptoError):
            decrypt(self.recipient, SigningKey.generate().verify_key, self.bound, envelope)

    def test_independent_passphrase_protects_recovery_key_and_wrong_password_fails(self) -> None:
        password = secrets.token_bytes(32)
        protected = wrap_key(self.recipient, password)
        self.assertNotIn(bytes(self.recipient), protected)
        self.assertEqual(bytes(unwrap_key(protected, password)), bytes(self.recipient))
        with self.assertRaises(CryptoError):
            unwrap_key(protected, secrets.token_bytes(32))

    def test_recovery_rotation_and_plaintext_absence_across_two_step_commit(self) -> None:
        with evidence_root(prefix="evernook-vault-") as temporary:
            root = Path(temporary)
            inbox = Inbox(root, self.sender.verify_key)
            envelope = seal(self.recipient.public_key, self.sender, self.bound, self.value)
            self.assertEqual(inbox.receive(envelope), "protected_pending_link")
            inbox.receive(envelope)
            with self.assertRaises(ValueError):
                inbox.read(self.bound)
            # Reopen after encrypted storage, before the ordinary link was committed.
            inbox.close()
            inbox = Inbox(root, self.sender.verify_key)
            self.assertEqual(inbox.recover_pending(), 1)
            self.assertEqual(inbox.recover_pending(), 0)
            self.assertEqual(decrypt(self.recipient, self.sender.verify_key, self.bound, inbox.read(self.bound)), self.value)
            rotated = PrivateKey.generate()
            newer = replace(self.bound, version=2, key_id="key-2")
            inbox.receive(seal(rotated.public_key, self.sender, newer, self.value))
            inbox.link(newer)
            with self.assertRaises(CryptoError):
                decrypt(self.recipient, self.sender.verify_key, newer, inbox.read(newer))
            self.assertEqual(decrypt(rotated, self.sender.verify_key, newer, inbox.read(newer)), self.value)
            inbox.backup(root / "backup.sqlite")
            recovery_password = secrets.token_bytes(32)
            protected_keys = [wrap_key(key, recovery_password) for key in [self.recipient, rotated]]
            restore = root / "restored"
            restore.mkdir()
            shutil.copy2(root / "backup.sqlite", restore / "inbox.sqlite")
            restored = Inbox(restore, self.sender.verify_key)
            try:
                for bound, bundle in zip([self.bound, newer], protected_keys):
                    self.assertEqual(decrypt(unwrap_key(bundle, recovery_password), self.sender.verify_key, bound, restored.read(bound)), self.value)
                with self.assertRaises(CryptoError):
                    decrypt(PrivateKey.generate(), self.sender.verify_key, self.bound, restored.read(self.bound))
            finally:
                restored.close()
            for path in root.iterdir():
                if path.is_file():
                    self.assertNotIn(self.value, path.read_bytes())
            inbox.close()

    def test_authorization_is_bound_signed_and_concurrently_consumed_once(self) -> None:
        with evidence_root(prefix="evernook-vault-") as temporary:
            inbox = Inbox(Path(temporary), self.sender.verify_key)
            inbox.receive(seal(self.recipient.public_key, self.sender, self.bound, self.value))
            inbox.link(self.bound)
            client = SigningKey.generate()
            helper = Authority(inbox, self.recipient, {"synthetic-harness": client.verify_key})
            request = Request(self.bound, "synthetic-harness", "read", "synthetic one-time operation")
            try:
                with self.assertRaises(PermissionError):
                    helper.grant_after_local_confirmation(request, approved=False)
                token = helper.grant_after_local_confirmation(request, approved=True)
                signature = client.sign(helper.claim(token, request)).signature
                for changed in [replace(request, client="unknown"), replace(request, purpose="different"), replace(request, operation="export"), replace(request, bound=replace(self.bound, version=2))]:
                    with self.assertRaises(PermissionError):
                        helper.deliver(token, changed, signature)
                with self.assertRaises(CryptoError):
                    helper.deliver(token, request, SigningKey.generate().sign(helper.claim(token, request)).signature)

                def consume(_: int) -> bool:
                    try:
                        return helper.deliver(token, request, signature) == self.value
                    except PermissionError:
                        return False

                with ThreadPoolExecutor(max_workers=8) as pool:
                    self.assertEqual(sum(pool.map(consume, range(8))), 1)
            finally:
                helper.lock()
                inbox.close()

    def test_real_process_exit_before_and_after_link_recovers_without_private_key_in_writer(self) -> None:
        for stage, expected_pending in [("before-link", 1), ("after-link", 0)]:
            with self.subTest(stage=stage), evidence_root(prefix="evernook-vault-crash-") as temporary:
                root = Path(temporary)
                envelope = seal(self.recipient.public_key, self.sender, self.bound, self.value)
                (root / "input.encrypted").write_bytes(envelope.serialize())
                (root / "sender.public").write_bytes(bytes(self.sender.verify_key))
                process = subprocess.run([sys.executable, str(Path(__file__).with_name("crash_worker.py")), str(root), stage],
                                         capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, 86)
                self.assertEqual(process.stdout.strip(), "synthetic-durable-checkpoint")
                inbox = Inbox(root, self.sender.verify_key)
                try:
                    self.assertEqual(inbox.recover_pending(), expected_pending)
                    self.assertEqual(decrypt(self.recipient, self.sender.verify_key, self.bound, inbox.read(self.bound)), self.value)
                finally:
                    inbox.close()

    def test_local_gui_password_entry_and_local_only_display_with_synthetic_input(self) -> None:
        import tkinter as tk
        from local_ui import LocalView

        password = secrets.token_urlsafe(32)
        bundle = wrap_key(self.recipient, password.encode())
        envelope = seal(self.recipient.public_key, self.sender, self.bound, self.value)
        root = tk.Tk()
        root.withdraw()  # Construct actual Tk widgets without showing an unattended password prompt.
        view = LocalView(root, "Synthetic object v1", lambda supplied: decrypt(unwrap_key(bundle, supplied), self.sender.verify_key, self.bound, envelope))
        try:
            self.assertEqual(view.password.cget("show"), "*")
            view.password.insert(0, "synthetic-wrong-password")
            view.confirm.invoke()
            self.assertEqual(view.result.cget("text"), "Unlock refused")
            view.password.insert(0, password)
            view.confirm.invoke()
            self.assertEqual(view.password.get(), "")
            self.assertEqual(view.result.cget("text"), self.value.decode())
            view.clear()
            self.assertEqual(view.result.cget("text"), "Locked")
        finally:
            root.destroy()

    def test_expiry_revoke_restart_lock_and_version_change_invalidate_grants(self) -> None:
        with evidence_root(prefix="evernook-vault-") as temporary:
            inbox = Inbox(Path(temporary), self.sender.verify_key)
            inbox.receive(seal(self.recipient.public_key, self.sender, self.bound, self.value))
            inbox.link(self.bound)
            client = SigningKey.generate()
            now = [100.0]
            helper = Authority(inbox, self.recipient, {"synthetic-harness": client.verify_key}, clock=lambda: now[0])
            request = Request(self.bound, "synthetic-harness", "read", "synthetic operation")
            try:
                for invalidation in ["expiry", "revoke", "restart", "new-version", "lock"]:
                    token = helper.grant_after_local_confirmation(request, approved=True, ttl=1)
                    signature = client.sign(helper.claim(token, request)).signature
                    if invalidation == "expiry":
                        now[0] += 2
                    elif invalidation == "revoke":
                        helper.revoke(token)
                    elif invalidation == "restart":
                        helper = Authority(inbox, self.recipient, {"synthetic-harness": client.verify_key})
                    elif invalidation == "new-version":
                        newer = replace(self.bound, version=2)
                        inbox.receive(seal(self.recipient.public_key, self.sender, newer, self.value))
                        inbox.link(newer)
                    else:
                        helper.lock()
                    with self.assertRaises(PermissionError):
                        helper.deliver(token, request, signature)
                    if invalidation == "new-version":
                        request = replace(request, bound=newer)
            finally:
                helper.lock()
                inbox.close()


if __name__ == "__main__":
    unittest.main()
