from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize


class RuntimeStartupTests(unittest.TestCase):
    def test_initialized_reader_can_open_during_another_wal_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-startup-") as directory:
            config, access = initialize(Path(directory) / "runtime", client_id="synthetic", entry="test")
            runtime = MemoryRuntime(config)
            event = Event(Source("test", "synthetic", "main", "original"), "user", 0, None,
                          "complete", "合成原文：保留37.50及标点。")
            receipt = runtime.submit(access, event)
            settings = json.loads(config.read_text(encoding="utf-8"))
            settings["busy_timeout_ms"] = 0
            config.write_text(json.dumps(settings), encoding="utf-8")
            with closing(sqlite3.connect(config.parent / "memory.sqlite3", isolation_level=None)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                try:
                    reader = MemoryRuntime(config)
                    message = reader.read(access, receipt.message_id)
                    self.assertIsNotNone(message)
                    assert message is not None
                    self.assertEqual(message.event, event)
                    self.assertEqual(reader.status(access).messages, 1)
                finally:
                    writer.execute("ROLLBACK")
            self.assertEqual(reader.submit(access, event), receipt)

    def test_read_only_startup_still_rejects_foreign_identity_and_future_storage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evernook-startup-guards-") as directory:
            config, _ = initialize(Path(directory) / "runtime", client_id="synthetic", entry="test")
            with closing(sqlite3.connect(config.parent / "memory.sqlite3", isolation_level=None)) as db:
                db.execute("PRAGMA user_version=99")
            with self.assertRaisesRegex(MemoryFailure, "unsupported_storage_version"):
                MemoryRuntime(config)
            with closing(sqlite3.connect(config.parent / "memory.sqlite3", isolation_level=None)) as db:
                db.execute("PRAGMA user_version=5")
            (config.parent / "idempotency.key").write_bytes(b"x" * 32)
            with self.assertRaisesRegex(MemoryFailure, "ledger_identity_mismatch"):
                MemoryRuntime(config)


if __name__ == "__main__":
    unittest.main()
