"""Synthetic crash windows; no credentials are accepted as command-line arguments."""
from contextlib import contextmanager
from collections.abc import Iterator
import os
from pathlib import Path
import sys
import sqlite3
from unittest.mock import patch

from evernook import Event, MemoryRuntime, Source
from evernook.protected import ProtectedInbox

runtime = MemoryRuntime(Path(sys.argv[1]))
access = runtime.access_from_file(Path(sys.argv[2]))
inbox = ProtectedInbox(runtime, Path(sys.argv[3]), Path(sys.argv[4]))
event = Event(Source("synthetic", "crash", "main", sys.argv[5]), "user", 1, None,
              "complete", "password=synthetic-crash-inbox-sentinel-123")
if sys.argv[5] == "before-link":
    inbox.stage(access, event)
    os._exit(71)
original_connection = runtime._connection


@contextmanager
def exit_after_core_commit() -> Iterator[sqlite3.Connection]:
    with original_connection() as db:
        yield db
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='protected_links_v1'").fetchone()
        committed = exists and db.execute("""SELECT 1 FROM protected_links_v1 l JOIN messages m ON m.id=l.message_id
            WHERE json_extract(m.source, '$.entry')='synthetic' AND json_extract(m.source, '$.session')='crash'
            AND json_extract(m.source, '$.event')=?""", (sys.argv[5],)).fetchone()
    if committed:
        os._exit(72)


with patch.object(runtime, "_connection", exit_after_core_commit):
    inbox.submit(access, event)
raise AssertionError("crash window was not reached")
