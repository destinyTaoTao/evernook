from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.protected import initialize_protected_inbox
from evernook.vault_local import LocalDisplayRequest, LocalVaultWindow


class LocalVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-local-vault-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(config)
        self.password = b"synthetic local password 123"
        self.inbox = initialize_protected_inbox(self.runtime, self.root / "inbox", self.root / "keys", self.password)
        self.event = Event(Source("synthetic", "session", "main", "1"), "user", 1, None,
                           "complete", "合成🙂\r\npassword=synthetic-local-only-sentinel-123")
        self.receipt = self.inbox.submit(self.access, self.event)
        self.now = [100.0]
        self.desktop = [True]

    def request(self) -> LocalDisplayRequest:
        return LocalDisplayRequest(self.inbox, self.access, self.receipt.object_id, "合成本地核对", clock=lambda: self.now[0], desktop=lambda: self.desktop[0])

    def test_real_hidden_tk_password_control_only_displays_after_local_button_and_clears_on_close(self) -> None:
        root = tk.Tk()
        root.withdraw()
        request = self.request()
        view = LocalVaultWindow(root, request)
        try:
            self.assertEqual(view.password.cget("show"), "*")
            self.assertNotIn("sentinel", str(view.result.cget("text")))
            view.password.insert(0, self.password.decode())
            view.confirm.invoke()
            self.assertEqual(str(view.result.cget("text")), self.event.text)
            self.assertEqual(view.password.get(), "")
            self.assertEqual(str(view.confirm.cget("state")), "disabled")
        finally:
            view.close()
        with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
            request.display_once(self.password, lambda _: self.fail("closed window request reused"))
        contents = b"".join(path.read_bytes() for path in self.root.rglob("*") if path.is_file())
        self.assertNotIn(b"synthetic-local-only-sentinel-123", contents)
        self.assertNotIn(self.password, contents)

    def test_wrong_password_request_expiry_cancel_desktop_change_and_replay_never_deliver(self) -> None:
        displayed: list[str] = []
        wrong = self.request()
        with self.assertRaisesRegex(MemoryFailure, "vault_unlock_refused"):
            wrong.display_once(b"synthetic wrong password 123", displayed.append)
        with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
            wrong.display_once(self.password, displayed.append)
        expired = self.request()
        self.now[0] += 121
        with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
            expired.display_once(self.password, displayed.append)
        canceled = self.request()
        canceled.cancel()
        with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
            canceled.display_once(self.password, displayed.append)
        changed = self.request()
        self.desktop[0] = False
        with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
            changed.display_once(self.password, displayed.append)
        self.assertEqual(displayed, [])

    def test_concurrent_consumption_delivers_at_most_once_and_audit_has_only_ids_result_and_time(self) -> None:
        request = self.request()
        displayed: list[str] = []
        def consume(_: int) -> str:
            try:
                request.display_once(self.password, displayed.append)
                return "displayed"
            except MemoryFailure as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(consume, range(4)))
        self.assertEqual(results.count("displayed"), 1)
        self.assertEqual(displayed, [self.event.text])
        with closing(sqlite3.connect(self.root / "inbox/inbox.sqlite3")) as db:
            rows = db.execute("SELECT * FROM local_display_audit_v1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("sentinel", str(rows))
        self.assertNotIn("合成本地核对", str(rows))

    def test_suppression_after_confirmation_prevents_display_and_source_cannot_be_replaced(self) -> None:
        request = self.request()
        evidence = self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, event="control"), text="合成：暂停普通召回", sensitive=False))
        assert self.receipt.message is not None
        self.runtime.change(self.access, Change("synthetic-suppression", self.receipt.message.message_id, 1, "suppress", evidence.message_id, "synthetic"))
        with self.assertRaisesRegex(MemoryFailure, "vault_source_unavailable"):
            request.display_once(self.password, lambda _: self.fail("suppressed original displayed"))
        with self.assertRaisesRegex(MemoryFailure, "vault_source_unavailable"):
            self.request()

    def test_cancel_during_unlock_or_reference_change_prevents_delivery(self) -> None:
        import evernook.vault_local as helper
        request = self.request()
        original = helper._unlock
        def cancel_during_unlock(*args: object) -> object:
            result = original(self.inbox, self.password)
            request.cancel()
            return result
        with patch.object(helper, "_unlock", cancel_during_unlock):
            with self.assertRaisesRegex(MemoryFailure, "local_display_expired"):
                request.display_once(self.password, lambda _: self.fail("canceled request displayed"))
        changed = self.request()
        with closing(sqlite3.connect(self.runtime.root / "memory.sqlite3", isolation_level=None)) as db:
            db.execute("UPDATE protected_links_v1 SET reference='{}'")
        with self.assertRaisesRegex(MemoryFailure, "vault_request_changed"):
            changed.display_once(self.password, lambda _: self.fail("changed reference displayed"))

    def test_ordinary_writer_can_commit_during_local_authorization_audit_before_display_lock(self) -> None:
        request = self.request()
        audit = request._audit
        def audit_with_concurrent_writer(result: str) -> None:
            event = replace(self.event, source=replace(self.event.source, event="ordinary-during-authorization"), text="合成：授权期间普通写入仍然可用")
            with ThreadPoolExecutor(max_workers=1) as pool:
                saved = pool.submit(self.runtime.submit, self.access, event).result(timeout=15)
            self.assertTrue(saved.original_saved)
            audit(result)
        displayed: list[str] = []
        with patch.object(request, "_audit", audit_with_concurrent_writer):
            request.display_once(self.password, displayed.append)
        self.assertEqual(displayed, [self.event.text])


if __name__ == "__main__":
    unittest.main()
