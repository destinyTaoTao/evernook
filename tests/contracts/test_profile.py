from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import Event, EvidenceSpan, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-profile-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, self.access = initialize(self.directory / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.profiles = ProfileRegistry(self.runtime)
        self.access_path = self.config.parent / "mcp-access-profile.json"
        from evernook import pair_client
        pair_client(self.config, client_id="synthetic", entry="test", access_path=self.access_path)
        self.number = 0

    def message(self, text: str, *, role: str = "user") -> EvidenceSpan:
        self.number += 1
        event = Event(Source("test", "session", "main", str(self.number)), role, self.number, None, "complete", text)
        receipt = self.runtime.submit(self.access, event)
        return EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)

    def draft(self, label: str = "喜欢摄影", *, category: str = "interest", confidence: float = 0.8,
              sources: tuple[EvidenceSpan, ...] | None = None) -> ProfileDraft:
        return ProfileDraft(category, label, confidence,
                            sources or (self.message(f"合成：用户提到{label}"),))

    def propose(self, tag_id: str, draft: ProfileDraft | None = None) -> Any:
        return self.profiles.decide(self.access, ProfileDecision(f"propose-{tag_id}", tag_id, 0, "propose",
                                                                 draft or self.draft()))

    def confirm(self, tag_id: str, revision: int, *, request_id: str | None = None) -> Any:
        confirmation = self.message("合成：确认这条画像标签")
        return self.profiles.decide(self.access, ProfileDecision(request_id or f"confirm-{tag_id}-{revision}",
            tag_id, revision, "confirm", confirmation_id=confirmation.message_id))

    def invoke(self, command: str, payload: object) -> dict[str, Any]:
        process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                                  "--access", str(self.access_path)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2])
        result: dict[str, Any] = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.stderr, b"")
        self.assertEqual(process.returncode, 0 if result["ok"] else 1)
        return result

    def test_cli_lifecycle_confirm_correct_suppress_and_summary_views(self) -> None:
        span = self.message("合成：用户提到喜欢摄影")
        source = {"message_id": span.message_id, "state_version": 1, "representation": "original",
                  "content_version": 1, "start": 0, "end": len(span.expected_text), "expected_text": span.expected_text}
        proposed = self.invoke("profile-decide", {"request_id": "cli-propose-1", "tag_id": "cli-tag-interest",
            "expected_revision": 0, "action": "propose",
            "draft": {"category": "interest", "label": "喜欢摄影", "confidence": 0.8, "sources": [source]}})
        self.assertEqual(proposed["result"]["state"], "draft")
        confirmed = self.invoke("profile-decide", {"request_id": "cli-confirm-1", "tag_id": "cli-tag-interest",
            "expected_revision": 1, "action": "confirm", "confirmation_id": self.message("合成：确认喜欢摄影").message_id})
        self.assertEqual(confirmed["result"]["state"], "confirmed")
        summary = self.invoke("profile-summary", {})
        self.assertEqual([tag["draft"]["label"] for tag in summary["result"]["tags"]], ["喜欢摄影"])
        corrected = self.invoke("profile-decide", {"request_id": "cli-correct-1", "tag_id": "cli-tag-interest",
            "expected_revision": 2, "action": "revise",
            "draft": {"category": "interest", "label": "喜欢胶片摄影", "confidence": 0.9,
                      "sources": [{"message_id": span.message_id, "state_version": 1, "representation": "original",
                                   "content_version": 1, "start": 0, "end": len(span.expected_text),
                                   "expected_text": span.expected_text}]}})
        self.assertEqual(corrected["result"]["state"], "draft")
        confirmed_two = self.invoke("profile-decide", {"request_id": "cli-confirm-2", "tag_id": "cli-tag-interest",
            "expected_revision": 3, "action": "confirm", "confirmation_id": self.message("合成：确认胶片摄影").message_id})
        self.assertEqual(confirmed_two["result"]["draft"]["label"], "喜欢胶片摄影")
        current = self.invoke("profile-read", {"tag_id": "cli-tag-interest"})
        self.assertEqual(current["result"]["revision"], 4)
        summary = self.invoke("profile-summary", {})
        self.assertEqual([tag["draft"]["label"] for tag in summary["result"]["tags"]], ["喜欢胶片摄影"])
        suppressed = self.invoke("profile-decide", {"request_id": "cli-suppress-1", "tag_id": "cli-tag-interest",
            "expected_revision": 4, "action": "suppress", "confirmation_id": self.message("合成：这条不对，先不注入").message_id})
        self.assertEqual(suppressed["result"]["state"], "suppressed")
        empty = self.invoke("profile-summary", {})
        self.assertEqual(empty["result"]["tags"], [])
        with_suppressed = self.invoke("profile-summary", {"include_suppressed": True})
        self.assertEqual([tag["state"] for tag in with_suppressed["result"]["tags"]], ["suppressed"])
        restored = self.invoke("profile-decide", {"request_id": "cli-restore-1", "tag_id": "cli-tag-interest",
            "expected_revision": 5, "action": "confirm", "confirmation_id": self.message("合成：恢复这条标签").message_id})
        self.assertEqual(restored["result"]["state"], "confirmed")
        history = self.invoke("profile-history", {"tag_id": "cli-tag-interest"})
        self.assertEqual([item["revision"] for item in history["result"]["records"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(history["result"]["records"][1]["draft"]["label"], "喜欢摄影")

    def test_idempotent_retry_conflicts_and_transition_guards(self) -> None:
        draft = self.draft()
        first = self.propose("tag-idem", draft)
        self.assertEqual(self.profiles.decide(self.access, ProfileDecision("propose-tag-idem", "tag-idem", 0,
            "propose", draft)), first)
        with self.assertRaisesRegex(MemoryFailure, "profile_request_conflict"):
            self.profiles.decide(self.access, ProfileDecision("propose-tag-idem", "tag-idem", 0, "propose", self.draft("另一个标签")))
        with self.assertRaisesRegex(MemoryFailure, "profile_revision_conflict"):
            self.confirm("tag-idem", 2)
        with self.assertRaisesRegex(MemoryFailure, "invalid_profile_transition"):
            self.profiles.decide(self.access, ProfileDecision("propose-again", "tag-idem", 1, "propose", self.draft()))
        with self.assertRaisesRegex(MemoryFailure, "invalid_profile_transition"):
            self.profiles.decide(self.access, ProfileDecision("suppress-draft", "tag-idem", 1, "suppress",
                                                              confirmation_id=self.message("合成：抑制草稿").message_id))
        self.confirm("tag-idem", 1)
        with self.assertRaisesRegex(MemoryFailure, "invalid_profile_transition"):
            self.profiles.decide(self.access, ProfileDecision("reject-confirmed", "tag-idem", 2, "reject",
                                                              confirmation_id=self.message("合成：拒绝已确认").message_id))

    def test_source_validation_and_protected_labels(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, "protected_profile"):
            self.propose("tag-secret", self.draft("password=synthetic-secret-2"))
        with self.assertRaisesRegex(MemoryFailure, "invalid_profile_draft"):
            self.propose("tag-confidence", self.draft(confidence=1.5))
        with self.assertRaisesRegex(MemoryFailure, "profile_span_mismatch"):
            real = self.message("合成：原文A")
            fake = EvidenceSpan(real.message_id, 1, "original", 1, 0, len(real.expected_text), "合成：原文B")
            self.propose("tag-mismatch", self.draft(sources=(fake,)))
        stale = self.message("合成：稍后被抑制的来源")
        self.propose("tag-stale", self.draft(sources=(stale,)))
        self.number += 1
        control = Event(Source("test", "session", "main", str(self.number)), "user", self.number, None, "complete", "合成：隐藏上一条")
        control_receipt = self.runtime.submit(self.access, control)
        from evernook import Change
        self.runtime.change(self.access, Change("suppress-" + stale.message_id, stale.message_id, 1, "suppress",
                                                control_receipt.message_id, "合成"))
        view = self.profiles.read(self.access, "tag-stale")
        self.assertEqual(view.state, "source_unavailable")
        self.assertEqual(view.error_code, "stale_profile_source")
        summary = self.profiles.summary(self.access)
        self.assertEqual(summary.tags, ())

    def test_confirmation_binding_time_and_one_message_one_tag(self) -> None:
        early = self.message("合成：过早的确认消息")
        self.propose("tag-binding")
        with self.assertRaisesRegex(MemoryFailure, "profile_confirmation_predates_review"):
            self.profiles.decide(self.access, ProfileDecision("confirm-early", "tag-binding", 1, "confirm",
                                                              confirmation_id=early.message_id))
        with self.assertRaisesRegex(MemoryFailure, "profile_user_confirmation_required"):
            self.profiles.decide(self.access, ProfileDecision("confirm-assistant", "tag-binding", 1, "confirm",
                                                              confirmation_id=self.message("合成：助手不算确认", role="assistant").message_id))
        confirmation = self.message("合成：确认绑定")
        self.profiles.decide(self.access, ProfileDecision("confirm-binding", "tag-binding", 1, "confirm",
                                                          confirmation_id=confirmation.message_id))
        self.propose("tag-other")
        with self.assertRaisesRegex(MemoryFailure, "profile_confirmation_conflict"):
            self.profiles.decide(self.access, ProfileDecision("confirm-other", "tag-other", 1, "confirm",
                                                              confirmation_id=confirmation.message_id))

    def test_category_filters_freeform_taxonomy_and_pagination(self) -> None:
        confirmed_interest = self.propose("tag-interest", self.draft("喜欢摄影", category="interest"))
        self.confirm("tag-interest", 1)
        self.propose("tag-hobby-freeform", self.draft("周末爬山", category="hobby"))
        self.confirm("tag-hobby-freeform", 1, request_id="confirm-freeform")
        filtered = self.profiles.summary(self.access, categories=("interest",))
        self.assertEqual([tag.tag_id for tag in filtered.tags], [confirmed_interest.tag_id])
        everything = self.profiles.summary(self.access)
        self.assertEqual(len(everything.tags), 2)
        self.assertEqual(everything.taxonomy_version, 1)
        rejected = self.propose("tag-rejected")
        self.profiles.decide(self.access, ProfileDecision("reject-rejected", "tag-rejected", 1, "reject",
                                                          confirmation_id=self.message("合成：拒绝这条").message_id))
        self.assertEqual([tag.tag_id for tag in self.profiles.summary(self.access).tags], ["tag-hobby-freeform", confirmed_interest.tag_id])
        for index in range(6):
            tag_id = f"tag-page-{index}"
            self.propose(tag_id, self.draft(f"标签{index}", category="preference"))
            self.confirm(tag_id, 1, request_id=f"confirm-page-{index}")
        page = self.profiles.history(self.access, "tag-interest", limit=1)
        self.assertEqual([record.revision for record in page.records], [1])
        self.assertEqual(page.next_offset, 1)


if __name__ == "__main__":
    unittest.main()
