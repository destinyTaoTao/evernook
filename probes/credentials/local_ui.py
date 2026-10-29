"""Local-only UI feasibility: passphrase stays in widget/helper memory, result stays in a label.

Not a secure desktop, human acceptance, production authorization API or hardened client pairing.
"""
from __future__ import annotations

from collections.abc import Callable
import tkinter as tk


class LocalView:
    def __init__(self, root: tk.Tk, description: str, decrypt_locally: Callable[[bytes], bytes]):
        root.title("EverNook R0 — synthetic local-only experiment")
        self.root = root
        self.decrypt_locally = decrypt_locally
        tk.Label(root, text=description + "\nDefault destination: this local window only. No harness delivery.").pack()
        self.password = tk.Entry(root, show="*", exportselection=False)
        self.password.pack()
        self.result = tk.Label(root, text="Locked")
        self.result.pack()
        self.confirm = tk.Button(root, text="Unlock and display locally", command=self._unlock)
        self.confirm.pack()

    def _unlock(self) -> None:
        password = self.password.get().encode("utf-8")
        self.password.delete(0, tk.END)
        try:
            value = self.decrypt_locally(password)
            self.result.config(text=value.decode("utf-8"))
        except Exception:
            self.result.config(text="Unlock refused")

    def clear(self) -> None:
        self.password.delete(0, tk.END)
        self.result.config(text="Locked")
