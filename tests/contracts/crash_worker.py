"""Synthetic process-kill fixture. Credentials are read locally, never command arguments."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
from unittest.mock import patch

from evernook import Access, AttachmentInput, Capture, Change, Event, ImportantMark, MemoryRuntime, Source


def main() -> None:
    config = Path(sys.argv[1])
    client = json.loads((config.parent / "client.json").read_text(encoding="utf-8"))
    access = Access(client["client_id"], client["token"])
    runtime = MemoryRuntime(config)
    event = Event(Source("test", "crash-session", "main", "1"), "assistant", 1,
                  None, "partial", "合成中断片段\r\n🙂e\u0301")
    if sys.argv[2] == "bounded_oversize":
        captured = runtime.submit_capture(access, Capture(Event(Source("test", "crash-session", "main", "large"),
            "user", 2, None, "complete", "x" * 1048577), "message"))
        print(json.dumps({"gaps": captured.gaps, "original_saved": captured.message.original_saved}), flush=True)
        return
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(*args, **kwargs)

        def before_commit(statement: str) -> None:
            if statement.strip().upper() == "COMMIT":
                os._exit(86)

        connection.set_trace_callback(before_commit)
        return connection

    if sys.argv[2].startswith("capture_"):
        input_receipt = runtime.submit(access, Event(Source("test", "crash-session", "main", "input"),
            "user", 0, None, "complete", "synthetic tool request"))
        capture = Capture(event, "tool_call", input_receipt.message_id, "synthetic-tool",
            (AttachmentInput("a1", "synthetic.txt", "text/plain", (b"synthetic", b" attachment")),))
        if sys.argv[2] == "capture_before_commit":
            with patch("sqlite3.connect", connect):
                runtime.submit_capture(access, capture)
            raise AssertionError("kill point not reached")
        capture_receipt = runtime.submit_capture(access, capture)
        print(json.dumps({"message_id": capture_receipt.message.message_id}), flush=True)
        os._exit(87)
    if sys.argv[2].startswith("change_"):
        target = runtime.submit(access, event)
        evidence = runtime.submit(access, Event(Source("test", "crash-session", "main", "evidence"),
            "user", 2, None, "complete", "synthetic explicit suppression"))
        request = Change("synthetic-change", target.message_id, 1, "suppress", evidence.message_id, "synthetic suppression")
        if sys.argv[2] == "change_before_commit":
            with patch("sqlite3.connect", connect):
                runtime.change(access, request)
            raise AssertionError("kill point not reached")
        state = runtime.change(access, request)
        print(json.dumps({"message_id": state.message_id, "version": state.version}), flush=True)
        os._exit(87)
    if sys.argv[2].startswith("important_"):
        receipt = runtime.submit(access, event)
        mark = ImportantMark(receipt.message_id, "original", 1, "synthetic-shared", None, None, event.text)
        if sys.argv[2] == "important_before_commit":
            with patch("sqlite3.connect", connect):
                runtime.mark_important(access, mark)
            raise AssertionError("kill point not reached")
        item = runtime.mark_important(access, mark)
        print(json.dumps({"important_id": item.important_id}), flush=True)
        os._exit(87)
    if sys.argv[2] == "before_commit":
        with patch("sqlite3.connect", connect):
            runtime.submit(access, event)
        raise AssertionError("kill point not reached")
    receipt = runtime.submit(access, event)
    print(json.dumps({"message_id": receipt.message_id}), flush=True)
    os._exit(87)


if __name__ == "__main__":
    main()
