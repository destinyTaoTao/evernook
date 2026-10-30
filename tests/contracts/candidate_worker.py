"""Synthetic process-exit fixture for candidate lease and completion transactions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
from unittest.mock import patch

from evernook import Access, CandidateDraft, CandidateWorkflow, EvidenceSpan, MemoryRuntime


def main() -> None:
    config, message_id, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    client = json.loads((config.parent / "client.json").read_text(encoding="utf-8"))
    access = Access(client["client_id"], client["token"])
    workflow = CandidateWorkflow(MemoryRuntime(config))
    lease = workflow.begin(access, (message_id,), processor_version=mode, lease_seconds=1)
    if mode == "after_claim":
        print(json.dumps({"run_id": lease.status.run_id}), flush=True)
        os._exit(85)
    assert lease.token is not None
    source = lease.sources[0]
    draft = CandidateDraft("synthetic recovered candidate", "user_statement", (
        EvidenceSpan(message_id, source.state_version, "original", 1, 0, len(source.message.event.text), source.message.event.text),))
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db: sqlite3.Connection = real_connect(*args, **kwargs)

        def before_commit(statement: str) -> None:
            if statement.strip().upper() == "COMMIT":
                os._exit(86)

        db.set_trace_callback(before_commit)
        return db

    if mode == "before_complete_commit":
        with patch("sqlite3.connect", connect):
            workflow.complete(access, lease.status.run_id, lease.token, (draft,))
        raise AssertionError("candidate completion crash point not reached")
    workflow.complete(access, lease.status.run_id, lease.token, (draft,))
    print(json.dumps({"run_id": lease.status.run_id}), flush=True)
    os._exit(87)


if __name__ == "__main__":
    main()
