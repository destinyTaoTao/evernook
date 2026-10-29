from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client


class PairingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-pairing-")
        self.addCleanup(temporary.cleanup)
        self.config, self.owner = initialize(Path(temporary.name) / "data", client_id="synthetic-owner",
                                            entry="owner-entry", allow_archive_review=True)
        self.peer_path = self.config.parent / "mcp-access-peer.json"

    def test_local_pairing_retries_share_identity_and_permission_is_bound_to_each_client(self) -> None:
        existing_runtime = MemoryRuntime(self.config)
        peer = pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        self.assertEqual(pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path), peer)
        self.assertEqual(existing_runtime.access_from_file(self.peer_path), peer)
        event = Event(Source("owner-entry", "session", "main", "1"), "user", 1, None, "complete", "synthetic evidence")
        receipt = existing_runtime.submit(self.owner, event)
        self.assertIsNotNone(existing_runtime.read(peer, receipt.message_id))
        self.assertTrue(existing_runtime.handshake(self.owner)["archive_review"])
        self.assertFalse(existing_runtime.handshake(peer)["archive_review"])
        with self.assertRaisesRegex(MemoryFailure, "archive_forbidden"):
            existing_runtime.review_archive(peer, receipt.message_id)
        self.assertEqual(existing_runtime.review_archive(self.owner, receipt.message_id).message.event, event)
        with self.assertRaisesRegex(MemoryFailure, "unauthorized_source"):
            existing_runtime.submit(peer, event)

    def test_concurrent_pairing_serializes_registry_and_rejects_conflicting_sources_or_paths(self) -> None:
        def pair(_: int) -> object:
            return pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)

        with ThreadPoolExecutor(max_workers=3) as pool:
            identities = list(pool.map(pair, range(3)))
        self.assertTrue(all(identity == identities[0] for identity in identities))
        for client_id, entry in [("synthetic-peer", "different"), ("different", "peer-entry")]:
            with self.assertRaisesRegex(MemoryFailure, "client_conflict"):
                pair_client(self.config, client_id=client_id, entry=entry, access_path=self.peer_path)
        with self.assertRaisesRegex(MemoryFailure, "invalid_access_location"):
            pair_client(self.config, client_id="third", entry="third", access_path=self.config)
        self.assertEqual(MemoryRuntime(self.config).access_from_file(self.peer_path), identities[0])

    def test_bootstrap_rejects_duplicate_json_keys_and_changed_access_file_without_overwriting(self) -> None:
        peer = pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        original = self.peer_path.read_text(encoding="utf-8")
        self.peer_path.write_text(original[:-1] + ',"contract_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "invalid_config"):
            MemoryRuntime(self.config).access_from_file(self.peer_path)
        self.peer_path.write_text(json.dumps({**asdict(peer), "contract_version": True}), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "access_file_conflict"):
            pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        changed = replace(peer, token="synthetic-wrong-value")
        self.peer_path.write_text(json.dumps(asdict(changed)), encoding="utf-8")
        before = (self.config.parent / "client.json").read_bytes()
        with self.assertRaisesRegex(MemoryFailure, "access_file_conflict"):
            pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        self.assertEqual((self.config.parent / "client.json").read_bytes(), before)
        self.assertEqual(json.loads(self.peer_path.read_text(encoding="utf-8")), asdict(changed))

    def test_access_file_write_failure_can_retry_without_changing_existing_owner_or_peer_identity(self) -> None:
        real_replace = os.replace
        calls = 0

        def fail_access(source: str | Path, target: str | Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic access-file failure")
            real_replace(source, target)

        with patch("os.replace", fail_access):
            with self.assertRaisesRegex(MemoryFailure, "^client_provision_failed$"):
                pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        self.assertFalse(self.peer_path.exists())
        self.assertTrue(MemoryRuntime(self.config).handshake(self.owner)["archive_review"])
        peer = pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        reopened = MemoryRuntime(self.config)
        self.assertEqual(reopened.access_from_file(self.peer_path), peer)
        self.assertEqual(pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path), peer)
        self.assertFalse(reopened.handshake(peer)["archive_review"])

    def test_multi_client_registry_rejects_duplicate_identities_entries_and_unknown_grants(self) -> None:
        pair_client(self.config, client_id="synthetic-peer", entry="peer-entry", access_path=self.peer_path)
        path = self.config.parent / "client.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        rows = original["clients"]
        for invalid in [
            {"schema_version": 3, "clients": [rows[0], rows[0]]},
            {"schema_version": 3, "clients": [rows[0], {**rows[1], "entry": rows[0]["entry"]}]},
            {"schema_version": 3, "clients": [{**rows[0], "vault": True}]},
            {"schema_version": 3, "clients": [{**rows[0], "archive_review": "true"}]},
        ]:
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(MemoryFailure, "invalid_credentials"):
                MemoryRuntime(self.config)
        path.write_text(json.dumps(original), encoding="utf-8")
        self.assertTrue(MemoryRuntime(self.config).handshake(self.owner)["archive_review"])


if __name__ == "__main__":
    unittest.main()
