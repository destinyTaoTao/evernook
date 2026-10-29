from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast
import unittest

from evernook import Access, Change, DerivedContext, Event, ImportantMark, MemoryFailure, MemoryRuntime, Source, initialize


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-boundary-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "1"), "user", 0, None, "complete", "synthetic ordinary text")

    def test_all_operations_require_matching_identity_token_and_contract(self) -> None:
        operations: list[Callable[[Access], object]] = [
            self.runtime.handshake, self.runtime.status,
            lambda access: self.runtime.submit(access, self.event),
            lambda access: self.runtime.read(access, "id"),
            lambda access: self.runtime.read_source(access, self.event.source),
            lambda access: self.runtime.list_messages(access),
            lambda access: self.runtime.mark_important(access, ImportantMark("id", "original", 1, "shared", 0, 1, "x")),
            lambda access: self.runtime.read_important(access, "id"),
            lambda access: self.runtime.state(access, "id"),
            lambda access: self.runtime.change(access, Change("change", "id", 1, "suppress", "evidence", "synthetic")),
            lambda access: self.runtime.review_archive(access, "id"),
            lambda access: self.runtime.publish_context(access, DerivedContext("result", "id", 1, "synthetic")),
            lambda access: self.runtime.read_context(access, "result"),
        ]
        for access, error in [(replace(self.access, token="wrong"), "unauthorized"),
                              (replace(self.access, client_id="wrong"), "unauthorized"),
                              (replace(self.access, contract_version=2), "incompatible_contract")]:
            for operation in operations:
                with self.assertRaisesRegex(MemoryFailure, error):
                    operation(access)
        with self.assertRaisesRegex(MemoryFailure, "unauthorized_source"):
            self.runtime.submit(self.access, replace(self.event, source=replace(self.event.source, entry="forged")))
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        self.assertEqual(self.runtime.handshake(self.access)["contract_version"], 1)

    def test_config_and_client_schema_reject_unknown_or_ambiguous_values(self) -> None:
        original = self.config.read_text(encoding="utf-8")
        config = json.loads(original)
        config["unknown"] = True
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "unsupported_config"):
            MemoryRuntime(self.config)
        self.config.write_text(original[:-1] + ',"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "invalid_config"):
            MemoryRuntime(self.config)
        self.config.write_text(original, encoding="utf-8")
        client_path = self.config.parent / "client.json"
        client = json.loads(client_path.read_text(encoding="utf-8"))
        client["schema_version"] = True
        client_path.write_text(json.dumps(client), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "invalid_credentials"):
            MemoryRuntime(self.config)

    def test_invalid_event_encoding_and_metadata_fail_without_storage_or_echoing_text(self) -> None:
        cases = [
            (replace(self.event, text="synthetic\ud800"), "invalid_encoding"),
            (replace(self.event, occurred_at="2021-01-01T00:00:00"), "invalid_time"),
            (replace(self.event, sequence=-1), "invalid_event"),
            (replace(self.event, role=cast(str, [])), "invalid_event"),
            (replace(self.event, text="a" * 1048577), "message_too_large"),
        ]
        for event, code in cases:
            with self.assertRaisesRegex(MemoryFailure, "^" + code + "$"):
                self.runtime.submit(self.access, event)
        with self.assertRaisesRegex(MemoryFailure, "invalid_encoding"):
            self.runtime.read_source(self.access, replace(self.event.source, event="bad\ud800"))
        self.assertEqual(self.runtime.status(self.access).messages, 0)
        self.assertEqual(self.runtime.status(self.access).pending_jobs, 0)

    def test_runtime_paths_cannot_use_checkout_relative_or_retargeted_config(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, "absolute_runtime_path_required"):
            initialize(Path("relative"), client_id="synthetic", entry="test")
        with self.assertRaisesRegex(MemoryFailure, "runtime_inside_checkout"):
            initialize(Path(__file__).resolve().parents[2] / "forbidden-runtime", client_id="synthetic", entry="test")
        relocated = self.config.parent.parent / "runtime.json"
        relocated.write_bytes(self.config.read_bytes())
        with self.assertRaisesRegex(MemoryFailure, "invalid_config_location"):
            MemoryRuntime(relocated)
        with self.assertRaises(FileExistsError):
            initialize(self.config.parent, client_id="synthetic", entry="test")

    def test_reopening_rejects_runtime_credentials_exposed_to_other_local_users(self) -> None:
        icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
        subprocess.run([str(icacls), str(self.config.parent / "client.json"), "/grant", "*S-1-1-0:R"],
                       capture_output=True, check=True, timeout=10)
        with self.assertRaisesRegex(MemoryFailure, "unsafe_runtime_permissions"):
            MemoryRuntime(self.config)


if __name__ == "__main__":
    unittest.main()
