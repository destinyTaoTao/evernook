"""Explicit local multi-entry private recording, without a model or host transcript."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from typing import Any
from uuid import UUID, uuid4

from .models import Access, MemoryFailure
from .private_capture_local import LocalPrivateCapture, _ProtectedClient
from .runtime import _identifier
from .vault_local import input_desktop_available


class LocalPrivateSession:
    """One bounded window; uncertain records require unchanged retries within the window."""

    def __init__(self, client: _ProtectedClient, access: Access, entry: str, *, session_id: str | None = None,
                 pending_event_id: str | None = None, sequence: int = 0):
        _identifier(entry)
        if type(sequence) is not int or not 0 <= sequence < 100:
            raise MemoryFailure('invalid_private_source')
        if (session_id is None) != (pending_event_id is None) or (session_id is None and sequence != 0):
            raise MemoryFailure('invalid_private_source')
        try:
            if session_id is not None and (str(UUID(session_id)) != session_id or str(UUID(pending_event_id)) != pending_event_id):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MemoryFailure('invalid_private_source') from None
        self.client, self.access, self.entry = client, access, entry
        self.session_id = session_id or str(uuid4())
        self._sequence, self._saved = sequence, 0
        self._current = (LocalPrivateCapture(client, access, entry, session_id=self.session_id,
                         event_id=pending_event_id, sequence=sequence) if pending_event_id else None)
        self._resuming = pending_event_id is not None
        self._busy = False
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, text: str) -> None:
        with self._lock:
            if self._closed:
                raise MemoryFailure('private_session_closed')
            if self._busy:
                raise MemoryFailure('private_session_busy')
            if self._sequence >= 100:
                raise MemoryFailure('private_session_limit')
            if self._current is None:
                self._current = LocalPrivateCapture(self.client, self.access, self.entry, session_id=self.session_id,
                                                    event_id=str(uuid4()), sequence=self._sequence)
            current = self._current
            self._busy = True
        try:
            current.submit(text)
            with self._lock:
                if not self._closed:
                    self._saved += 1
                    self._sequence += 1
                    self._resuming = False
                    current.close()
                    self._current = None
        finally:
            with self._lock:
                self._busy = False

    def close(self) -> dict[str, Any]:
        with self._lock:
            self._closed = True
            if self._current is not None:
                current = self._current.close()
                if current['state'] == 'saved':
                    self._saved += 1
                    self._sequence += 1
                    self._resuming = False
                    self._current = None
            else:
                current = None
            result: dict[str, Any] = {'state': 'saved' if self._saved else 'canceled',
                                      'session_id': self.session_id, 'saved_count': self._saved}
            if current is not None:
                if current['state'] == 'outcome_unknown' or self._resuming:
                    result.update(state='outcome_unknown', pending_event_id=current['event_id'], sequence=self._sequence)
            return result


class LocalPrivateSessionWindow:
    def __init__(self, root: tk.Tk, session: LocalPrivateSession):
        self.root, self.session = root, session
        self.closed = False
        self.results: queue.Queue[str] = queue.Queue(maxsize=1)
        root.title('EverNook — 本地私密记录会话')
        root.geometry('560x420')
        tk.Label(root, text='每条记录单独加密保存，不发送至聊天或模型。\n不支持流或附件；关闭或失焦会清空输入。',
                 justify='left', wraplength=520).pack(padx=16, pady=10)
        tk.Label(root, text=f'私密会话编号：{session.session_id}', wraplength=520).pack(padx=16)
        self.text = tk.Text(root, height=10, width=60, wrap='word', exportselection=False)
        self.text.pack(padx=16, pady=6, fill='both', expand=True)
        hint = ('恢复未确认记录：请重新输入与原来完全相同的内容。'
                if session._resuming else '输入一条私密记录后点击保存。')
        self.status = tk.Label(root, text=hint, wraplength=520)
        self.status.pack(padx=16, pady=6)
        self.save = tk.Button(root, text='加密保存本条', command=self._submit)
        self.save.pack(pady=4)
        tk.Button(root, text='结束会话并清空', command=self.close).pack(pady=4)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<FocusOut>', self._focus_lost)
        self.poll_id = self.root.after(200, self._poll)

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
                self.session.submit(text)
                result = 'saved'
            except MemoryFailure as error:
                result = 'limit' if error.code == 'private_session_limit' else 'unknown'
            except Exception:
                result = 'unknown'
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
            if result == 'saved':
                self.text.configure(state='normal')
                self.text.delete('1.0', 'end')
                self.status.configure(text='本条已加密保存；可输入下一条。')
                self.save.configure(text='加密保存本条', state='normal')
            elif result == 'limit':
                self.status.configure(text='本窗口已达 100 条上限，请结束并新建会话。')
            else:
                self.status.configure(text='未能确认本条结果；只能重试相同内容。')
                self.save.configure(text='重试相同内容', state='normal')
        self.poll_id = self.root.after(200, self._poll)

    def _focus_lost(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.root:
            self.root.after(100, self._check_focus)

    def _check_focus(self) -> None:
        if not self.closed:
            focused = self.root.focus_displayof()
            if focused is None or focused.winfo_toplevel() is not self.root:
                self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.session.close()
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def show_private_session(session: LocalPrivateSession) -> dict[str, Any]:
    if not input_desktop_available():
        raise MemoryFailure('private_capture_desktop_unavailable')
    root = tk.Tk()
    LocalPrivateSessionWindow(root, session)
    root.attributes('-topmost', True)
    root.lift()
    root.after_idle(root.focus_force)
    root.mainloop()
    return session.close()
