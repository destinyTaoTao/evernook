from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evernook import Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.management import MemoryManagement
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class ManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-management-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.ui = MemoryManagement(self.runtime)
        self.profiles = ProfileRegistry(self.runtime)
        self.seq = 0

    def source(self, text: str = "合成：喜欢观鸟🙂") -> EvidenceSpan:
        self.seq += 1
        receipt = self.runtime.submit(self.access, Event(Source("owner", "synthetic-session", "main", str(self.seq)),
                                                        "user", self.seq, None, "complete", text))
        return EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)

    def draft(self, tag_id: str = "birding") -> str:
        span = self.source()
        self.profiles.decide(self.access, ProfileDecision("draft-" + tag_id, tag_id, 0, "propose",
                              ProfileDraft("interest", "喜欢观鸟", 0.8, (span,))))
        return span.message_id

    def apply(self, operation: dict[str, str]) -> dict[str, object]:
        preview = self.ui.preview(self.access, operation)
        return self.ui.confirm(self.access, preview["proposal_id"], operation)

    def test_profile_review_revision_suppression_and_original_retention(self) -> None:
        source_id = self.draft()
        self.assertEqual(len(self.ui.read(self.access, view="drafts")["items"]), 1)
        self.apply({"kind": "profile_confirm", "tag_id": "birding"})
        self.assertEqual(len(self.ui.read(self.access, view="network_profiles", entry="owner", session_id="synthetic-session")["items"]), 1)
        self.assertEqual(self.ui.read(self.access, view="network_profiles", entry="other")["items"], [])
        self.assertEqual(self.ui.read(self.access, view="network_profiles", since="2020-01-01T00:00:00Z")["items"], [])
        items = self.ui.read(self.access, view="profiles")["items"]
        self.assertEqual(items[0]["state"], "confirmed")
        self.assertNotIn("expected_text", items[0]["draft"]["sources"][0])
        self.apply({"kind": "profile_revise", "tag_id": "birding", "label": "喜欢拍摄鸟类"})
        record = self.profiles.read(self.access, "birding").record
        assert record is not None
        self.assertEqual(record.draft.label, "喜欢拍摄鸟类")
        self.apply({"kind": "profile_suppress", "tag_id": "birding"})
        self.assertEqual(self.ui.read(self.access, view="profiles")["items"], [])
        self.assertIsNotNone(self.runtime.read(self.access, source_id))
        self.assertGreater(len(self.ui.read(self.access, view="history", tag_id="birding")["items"]), 2)

    def test_source_preview_stale_dependency_and_confirm_replay(self) -> None:
        source_id = self.draft()
        operation = {"kind": "source_suppress", "message_id": source_id}
        preview = self.ui.preview(self.access, operation)
        self.assertEqual(preview["affected"][0]["id"], "birding")
        self.apply({"kind": "profile_confirm", "tag_id": "birding"})
        with self.assertRaisesRegex(MemoryFailure, "management_preview_stale"):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        preview = self.ui.preview(self.access, operation)
        first = self.ui.confirm(self.access, preview["proposal_id"], operation)
        restarted = MemoryManagement(MemoryRuntime(self.config))
        self.assertEqual(restarted.confirm(self.access, preview["proposal_id"], operation), first)
        self.assertIsNone(self.runtime.read(self.access, source_id))
        self.assertEqual(self.ui.read(self.access, view="profiles")["items"], [])
        with self.assertRaisesRegex(MemoryFailure, "source_unavailable"):
            self.ui.read(self.access, view="source", message_id=source_id)

    def test_atomic_failure_leaves_no_confirmation_or_change(self) -> None:
        source_id = self.source().message_id
        operation = {"kind": "source_suppress", "message_id": source_id}
        preview = self.ui.preview(self.access, operation)
        before = self.runtime.status(self.access).messages
        with self.runtime._connection() as db:
            db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(MemoryFailure):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        self.assertEqual(self.runtime.status(self.access).messages, before)
        self.assertEqual(self.runtime.state(self.access, source_id).status, "active")
        with self.runtime._connection() as db:
            db.execute("DROP TRIGGER synthetic_failure")
        self.assertEqual(self.ui.confirm(self.access, preview["proposal_id"], operation)["state"], "suppressed")

    def test_permissions_expiry_payload_binding_and_cross_client(self) -> None:
        status = self.ui.read(self.access, view="status")
        self.assertEqual(status["configuration_schema_version"], 1)
        self.assertFalse(status["daily_admitted"])
        self.assertNotIn("token", json.dumps(status))
        ordinary_path = self.config.parent / "mcp-access-ordinary.json"
        pair_client(self.config, client_id="ordinary", entry="dsh", access_path=ordinary_path, scopes=("capture", "recall"))
        ordinary = self.runtime.access_from_file(ordinary_path)
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.ui.read(ordinary, view="status")
        source_id = self.source().message_id
        operation = {"kind": "source_correct", "message_id": source_id, "text": "合成：修正后的原话"}
        preview = self.ui.preview(self.access, operation)
        with self.assertRaisesRegex(MemoryFailure, "management_preview_mismatch"):
            self.ui.confirm(self.access, preview["proposal_id"], {**operation, "text": "另一个值"})
        other_path = self.config.parent / "mcp-access-other.json"
        pair_client(self.config, client_id="other", entry="other", access_path=other_path, scopes=("recall", "maintain", "accept"))
        with self.assertRaisesRegex(MemoryFailure, "management_preview_mismatch"):
            self.ui.confirm(self.runtime.access_from_file(other_path), preview["proposal_id"], operation)
        with self.runtime._connection() as db:
            db.execute("UPDATE management_previews_v1 SET expires=0 WHERE id=?", (preview["proposal_id"],))
        with self.assertRaisesRegex(MemoryFailure, "management_preview_expired"):
            self.ui.confirm(self.access, preview["proposal_id"], operation)
        self.apply(operation)
        self.assertEqual(self.runtime.state(self.access, source_id).status, "corrected")
        self.assertIsNone(self.runtime.read(self.access, source_id))

    def test_source_unicode_pagination_timeline_and_real_cli(self) -> None:
        text = "合成🙂" * 900
        source_id = self.source(text).message_id
        page = self.ui.read(self.access, view="source", message_id=source_id)
        tail = self.ui.read(self.access, view="source", message_id=source_id, cursor=page["next_cursor"])
        self.assertEqual(page["text"] + tail["text"], text)
        self.assertEqual(self.ui.read(self.access, view="timeline", session_id="other")["items"], [])
        self.assertEqual(self.ui.read(self.access, view="timeline", query="🙂")["items"][0]["message_id"], source_id)
        path = self.config.parent / "mcp-access-cli-ui.json"
        pair_client(self.config, client_id="cli-ui", entry="ui", access_path=path, scopes=("recall", "maintain", "accept"))
        result = subprocess.run([sys.executable, "-m", "evernook.cli", "manage", "--config", str(self.config),
                                 "--access", str(path)], input=b'{"action":"read","view":"status"}',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(json.loads(result.stdout)["result"]["daily_admitted"])
        filtered = subprocess.run([sys.executable, "-m", "evernook.cli", "manage", "--config", str(self.config),
                                   "--access", str(path)],
                                  input=b'{"action":"read","view":"timeline","entry":"owner","session_id":"synthetic-session"}',
                                  capture_output=True, timeout=30)
        self.assertEqual(filtered.returncode, 0, filtered.stdout)
        self.assertEqual(json.loads(filtered.stdout)["result"]["items"][0]["message_id"], source_id)
        with self.assertRaisesRegex(MemoryFailure, "invalid_management_operation"):
            self.ui.preview(self.access, {"kind": "purge", "message_id": source_id})

    def test_timeline_filters_entry_session_and_occurrence_time(self) -> None:
        def submit(session: str, event_id: str, occurred: str | None) -> str:
            return self.runtime.submit(self.access, Event(Source("owner", session, "main", event_id),
                "user", 1, occurred, "complete", "合成筛选记录")).message_id

        first = submit("session-a", "one", "2022-01-01T00:00:00Z")
        submit("session-b", "two", "2023-01-01T00:00:00Z")
        unknown = submit("session-a", "three", None)
        def ids(**kwargs: object) -> set[str]:
            return {item["message_id"] for item in self.ui.read(self.access, view="timeline", **kwargs)["items"]}
        self.assertEqual(ids(entry="owner", session_id="session-a"), {first, unknown})
        self.assertEqual(ids(entry="other"), set())
        self.assertEqual(ids(since="2022-01-01T08:00:00+08:00", until="2023-01-01T00:00:00Z"), {first})
        page = self.ui.read(self.access, view="timeline", entry="owner", session_id="session-a", limit=1)
        self.assertIsNotNone(page["next_offset"])
        tail = self.ui.read(self.access, view="timeline", entry="owner", session_id="session-a", limit=1, offset=page["next_offset"])
        self.assertEqual({page["items"][0]["message_id"], tail["items"][0]["message_id"]}, {first, unknown})
        with self.assertRaisesRegex(MemoryFailure, "invalid_time_range"):
            ids(since="2023-01-01T00:00:00Z", until="2023-01-01T00:00:00Z")
        self.apply({"kind": "source_suppress", "message_id": first})
        self.assertEqual(ids(since="2022-01-01T00:00:00Z", until="2023-01-01T00:00:00Z"), set())

    def test_canceled_preview_stays_canceled_after_restart_and_cannot_confirm(self) -> None:
        source_id = self.source().message_id
        operation = {'kind': 'source_suppress', 'message_id': source_id}
        preview = self.ui.preview(self.access, operation)
        canceled = self.ui.cancel(self.access, preview['proposal_id'])
        restarted = MemoryManagement(MemoryRuntime(self.config))
        self.assertEqual(restarted.cancel(self.access, preview['proposal_id']), canceled)
        with self.assertRaisesRegex(MemoryFailure, 'management_preview_canceled'):
            restarted.confirm(self.access, preview['proposal_id'], operation)
        self.assertIsNotNone(self.runtime.read(self.access, source_id))
        preview = self.ui.preview(self.access, operation)
        committed = self.ui.confirm(self.access, preview['proposal_id'], operation)
        self.assertEqual(self.ui.cancel(self.access, preview['proposal_id']), committed)


if __name__ == "__main__":
    unittest.main()
