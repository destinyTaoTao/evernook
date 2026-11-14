"""Local-only, single-request display helper. Never imported by ordinary query adapters."""
from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from functools import lru_cache
import threading
import time
import tkinter as tk
from uuid import uuid4
from typing import Any

from .models import Access, MemoryFailure
from .protected import ProtectedInbox
from .runtime import _identifier, _json
from .vault_crypto import _decrypt_event, _unlock


@lru_cache(maxsize=1)
def _load_user32() -> Any:
    return ctypes.WinDLL("user32", use_last_error=True)


@lru_cache(maxsize=1)
def _user32() -> Any:
    user = _load_user32()
    user.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    user.CloseDesktop.argtypes = (wintypes.HANDLE,)
    user.CloseDesktop.restype = wintypes.BOOL
    return user


def input_desktop_available() -> bool:
    """Conservative session and input-desktop guard, not physical identity proof."""
    from .windows_session import current_session_unlocked
    if not current_session_unlocked():
        return False
    try:
        user = _user32()
        handle = user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not handle:
            return False
        try:
            name, required = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            return bool(user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(required))) and name.value == "Default"
        finally:
            user.CloseDesktop(handle)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class LocalDisplayRequest:
    """Trusted helper-owned request. No boolean approval flag, token API, or harness delivery."""

    def __init__(self, inbox: ProtectedInbox, access: Access, object_id: str, purpose: str, *,
                 clock: Callable[[], float] = time.monotonic, desktop: Callable[[], bool] = input_desktop_available):
        _identifier(purpose)
        if not purpose.strip():
            raise MemoryFailure("invalid_display_purpose")
        inbox.runtime._require_scope(access, 'recall', 'vault_local_display')
        receipt = inbox.status(access, object_id)
        if receipt.state != "protected_linked" or receipt.message is None:
            raise MemoryFailure("protected_link_unavailable")
        state = inbox.runtime.state(access, receipt.message.message_id)
        if state.status != "active":
            raise MemoryFailure("vault_source_unavailable")
        self.inbox, self.access, self.object_id, self.purpose = inbox, access, object_id, purpose
        self.message_id, self.version, self.state_version = receipt.message.message_id, receipt.version, state.version
        self.request_id = str(uuid4())
        self._clock, self._desktop = clock, desktop
        self._expires, self._used = clock() + 120, False
        self._canceled = False
        self._lock = threading.Lock()
        with inbox._connection() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
            value, _ = inbox._verified(row)
            self._bound = _json(value["bound"])
            self._envelope = str(row["envelope"])

    def cancel(self) -> None:
        with self._lock:
            self._used = True
            self._canceled = True

    def _audit(self, result: str) -> None:
        with self.inbox._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS local_display_audit_v1 (request_id TEXT PRIMARY KEY, object_id TEXT NOT NULL, client_id TEXT NOT NULL, operation TEXT NOT NULL, result TEXT NOT NULL, recorded_at TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO local_display_audit_v1 VALUES (?,?,?,'display_local',?,?)",
                (self.request_id, self.object_id, self.access.client_id, result, datetime.now(timezone.utc).isoformat()))
            db.execute("COMMIT")

    def _best_effort_audit(self, result: str) -> None:
        try:
            self._audit(result)
        except Exception:
            pass  # The display outcome must win over audit-storage availability.

    def display_once(self, password: bytes, sink: Callable[[str], None]) -> None:
        # The only production sink is a local label configured by this helper.
        with self._lock:
            if self._used or self._clock() >= self._expires or not self._desktop():
                self._used = True
                raise MemoryFailure("local_display_expired")
            self._used = True  # Wrong password, revoked state, and downstream failure also consume this request.
        try:
            self.inbox.runtime._require_scope(self.access, 'recall', 'vault_local_display')
            from .protected import _require_protected_available
            from .service_gate import ownership_gate
            # A missing-vault transition must not start while a key is being unlocked.
            with ownership_gate(self.inbox.runtime.root, exclusive=False, timeout=self.inbox.runtime._timeout):
                _require_protected_available(self.inbox.runtime)
                private = _unlock(self.inbox, password)
                with self.inbox._connection() as db:
                    row = db.execute("SELECT * FROM objects WHERE id=?", (self.object_id,)).fetchone()
                    if row is None or row["state"] != "protected_linked":
                        raise MemoryFailure("protected_link_unavailable")
                    event = _decrypt_event(self.inbox, row, private, expected_envelope=self._envelope, expected_bound=self._bound)
            self._best_effort_audit("authorization_attempted")
            self.inbox.runtime._require_scope(self.access, 'recall', 'vault_local_display')
            with self.inbox.runtime._connection() as core:
                core.execute("BEGIN IMMEDIATE")
                _require_protected_available(self.inbox.runtime, core)
                state = self.inbox.runtime._state(core, self.message_id)
                if state.status != "active" or state.version != self.state_version:
                    raise MemoryFailure("vault_source_unavailable")
                reference = core.execute("SELECT reference FROM protected_links_v1 WHERE message_id=?", (self.message_id,)).fetchone()
                if reference is None or reference["reference"] != self.inbox._reference(self.object_id, self._envelope):
                    raise MemoryFailure("vault_request_changed")
                with self._lock:
                    if self._canceled or self._clock() >= self._expires or not self._desktop():
                        raise MemoryFailure("local_display_expired")
                    sink(event.text)
                core.execute("COMMIT")
        except MemoryFailure:
            # The audit is best-effort: an unavailable audit store must never replace the real denial.
            self._best_effort_audit("denied")
            raise
        except Exception:
            self._best_effort_audit("denied")
            raise MemoryFailure("local_display_failed") from None


class LocalVaultWindow:
    def __init__(self, root: tk.Tk, request: LocalDisplayRequest):
        self.root, self.request = root, request
        self._closed = False
        root.title("EverNook — 本地单次查看")
        root.resizable(False, False)
        provenance = ('来源分片与时序未归档。' if request.version == 2 else '旧版来源完整性未知。')
        description = (f"调用端：{request.access.client_id}\n对象：{request.object_id}\n凭证版本：{request.version}\n{provenance}\n"
                       f"操作：仅本窗口展示\n用途：{request.purpose}\n明文不会交给 harness。关闭后需重新确认。")
        tk.Label(root, text=description, justify="left", wraplength=520).pack(padx=20, pady=12)
        self.password = tk.Entry(root, show="*", exportselection=False, width=48)
        self.password.pack(padx=20, pady=8)
        self.result = tk.Label(root, text="输入独立主口令并确认本次查看。", justify="left", wraplength=520)
        self.result.pack(padx=20, pady=12)
        self.confirm = tk.Button(root, text="确认本次用途并仅在本地展示", command=self._display)
        self.confirm.pack(pady=8)
        tk.Button(root, text="清除并关闭", command=self.close).pack(pady=8)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<FocusOut>", self._focus_lost)
        self._poll_id = root.after(500, self._poll)
        self._expire_id = root.after(120000, self.close)

    def _focus_lost(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.root:
            self.root.after(100, self._check_focus)

    def _check_focus(self) -> None:
        if self._closed:
            return
        focused = self.root.focus_displayof()
        if focused is None or focused.winfo_toplevel() is not self.root:
            self.close()

    def _poll(self) -> None:
        if self._closed:
            return
        if not self.request._desktop():
            self.close()
            return
        self._poll_id = self.root.after(500, self._poll)

    def _display(self) -> None:
        if self._closed:
            return
        password = self.password.get().encode("utf-8")
        self.password.delete(0, tk.END)
        self.confirm.configure(state="disabled")
        def local_label(text: str) -> None:
            self.result.configure(text=text)
        try:
            self.request.display_once(password, local_label)
            self.root.after_cancel(self._expire_id)
            self._expire_id = self.root.after(30000, self.close)
        except Exception:
            self.result.configure(text="本次查看被拒绝；关闭后可重新发起。")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.request.cancel()
        self.password.delete(0, tk.END)
        self.result.configure(text="已锁定")
        self.root.after_cancel(self._poll_id)
        self.root.after_cancel(self._expire_id)
        self.root.destroy()


def show_local(inbox: ProtectedInbox, access: Access, object_id: str, purpose: str) -> dict[str, object]:
    request = LocalDisplayRequest(inbox, access, object_id, purpose)
    root = tk.Tk()
    LocalVaultWindow(root, request)
    root.mainloop()
    return {"request_id": request.request_id, "destination": "local_window", "closed": True}
