"""Trusted local private-note input; never routed through a host chat or model."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from typing import Any, Protocol
from uuid import UUID, uuid4

from .models import Access, Event, MemoryFailure, Source
from .runtime import _identifier
from .vault_local import input_desktop_available


class _ProtectedClient(Protocol):
    def protected_receive(self, access: Access, event: Event, *, timeout: float = 30) -> dict[str, Any]: ...


class LocalPrivateCapture:
    """Freeze one event across retries; closing an in-flight operation is never reported as rollback."""

    def __init__(self, client: _ProtectedClient, access: Access, entry: str, *,
                 session_id: str | None = None, event_id: str | None = None):
        _identifier(entry)
        if (session_id is None) != (event_id is None):
            raise MemoryFailure('invalid_private_source')
        try:
            if session_id is not None and (str(UUID(session_id)) != session_id or str(UUID(event_id)) != event_id):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MemoryFailure('invalid_private_source') from None
        self.client, self.access, self.entry = client, access, entry
        self.session_id, self.event_id = session_id or str(uuid4()), event_id or str(uuid4())
        self._event: Event | None = None
        self._state = 'canceled'
        self._busy = False
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, text: str) -> None:
        with self._lock:
            if self._closed or self._state == 'saved':
                raise MemoryFailure('private_capture_closed')
            if self._busy:
                raise MemoryFailure('private_capture_busy')
            try:
                valid = isinstance(text, str) and bool(text.strip()) and len(text.encode('utf-8', errors='strict')) <= 1048576
            except UnicodeError:
                valid = False
            if not valid:
                raise MemoryFailure('invalid_private_input')
            if self._event is not None and text != self._event.text:
                raise MemoryFailure('private_capture_content_changed')
            if self._event is None:
                self._event = Event(Source(self.entry, self.session_id, 'private', self.event_id),
                                    'user', 0, None, 'complete', text, sensitive=True)
            event = self._event
            self._busy = True
            self._state = 'outcome_unknown'
        try:
            result = self.client.protected_receive(self.access, event)
            message = result.get('message')
            if (result.get('state') != 'protected_linked' or result.get('encrypted_original_saved') is not True
                    or not isinstance(message, dict) or message.get('original_saved') is not False):
                raise MemoryFailure('private_capture_unavailable')
            for value in (result.get('object_id'), result.get('inbox_id'), message.get('message_id')):
                if not isinstance(value, str) or str(UUID(value)) != value:
                    raise MemoryFailure('private_capture_unavailable')
            with self._lock:
                if not self._closed:
                    self._state = 'saved'
                    self._event = None
        except Exception:
            raise MemoryFailure('private_capture_unavailable') from None
        finally:
            with self._lock:
                self._busy = False

    def close(self) -> dict[str, str]:
        with self._lock:
            self._closed = True
            self._event = None
            return {'state': self._state, 'session_id': self.session_id, 'event_id': self.event_id}


class LocalPrivateWindow:
    def __init__(self, root: tk.Tk, request: LocalPrivateCapture):
        self.root, self.request = root, request
        self.closed = False
        self.results: queue.Queue[bool] = queue.Queue(maxsize=1)
        root.title('EverNook — 本地私密记录')
        root.geometry('560x400')
        tk.Label(root, text='仅加密保存到本地记忆，不发送到聊天或模型。\n关闭或失去焦点会清空窗口。',
                 justify='left', wraplength=520).pack(padx=16, pady=10)
        tk.Label(root, text=f'重试编号：{request.session_id} / {request.event_id}',
                 justify='left', wraplength=520).pack(padx=16)
        self.text = tk.Text(root, height=10, width=60, wrap='word', exportselection=False)
        self.text.pack(padx=16, pady=6, fill='both', expand=True)
        self.status = tk.Label(root, text='输入私密记录后，明确点击保存。', wraplength=520)
        self.status.pack(padx=16, pady=6)
        self.save = tk.Button(root, text='加密保存', command=self._submit)
        self.save.pack(pady=4)
        tk.Button(root, text='清空并关闭', command=self.close).pack(pady=4)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<FocusOut>', self._focus_lost)
        self.poll_id = root.after(200, self._poll)

    def _submit(self) -> None:
        if self.closed:
            return
        text = self.text.get('1.0', 'end-1c')
        if not text.strip():
            self.status.configure(text='请输入要加密保存的内容。')
            return
        self.text.configure(state='disabled')
        self.save.configure(state='disabled')
        self.status.configure(text='正在加密保存；请勿重复提交。')
        def worker() -> None:
            try:
                self.request.submit(text)
                result = True
            except Exception:
                result = False
            self.results.put(result)
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        if not input_desktop_available():
            self.close()
            return
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if result:
                self.text.configure(state='normal')
                self.text.delete('1.0', 'end')
                self.text.configure(state='disabled')
                self.status.configure(text='已加密保存。')
            else:
                self.status.configure(text='未能确认保存结果；可重试同一内容，关闭后结果可能不确定。')
                self.save.configure(text='重试相同内容', state='normal')
        self.poll_id = self.root.after(200, self._poll)

    def _focus_lost(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.root:
            self.root.after(100, self._check_focus)

    def _check_focus(self) -> None:
        if self.closed:
            return
        focused = self.root.focus_displayof()
        if focused is None or focused.winfo_toplevel() is not self.root:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.request.close()
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def show_private_capture(request: LocalPrivateCapture) -> dict[str, str]:
    if not input_desktop_available():
        raise MemoryFailure('private_capture_desktop_unavailable')
    root = tk.Tk()
    LocalPrivateWindow(root, request)
    root.attributes('-topmost', True)
    root.lift()
    root.after_idle(root.focus_force)
    root.mainloop()
    return request.close()
