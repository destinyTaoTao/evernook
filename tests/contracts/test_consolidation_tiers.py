from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import (ConsolidationDraft, ConsolidationWorkflow, Event, EvidenceSpan, HARNESS_DEFAULT_SCOPES,
                      MemoryFailure, MemoryRuntime, Source, initialize, pair_client)
from evernook.profile import ProfileDecision, ProfileRegistry


class ConsolidationTierTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-consolidation-tier-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config, _ = initialize(self.directory / "data", client_id="owner", entry="owner")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="owner", entry="owner", access_path=self.owner_path)
        self.access = self.runtime.access_from_file(self.owner_path)
        self.worker_path = self.config.parent / "mcp-access-worker.json"
        self.worker = pair_client(self.config, client_id="worker", entry="worker", access_path=self.worker_path,
                                  scopes=("recall", "capture", "maintain", "accept"))
        self.harness_path = self.config.parent / "mcp-access-harness.json"
        self.harness = pair_client(self.config, client_id="harness", entry="harness",
                                   access_path=self.harness_path, scopes=HARNESS_DEFAULT_SCOPES)
        self.workflow = ConsolidationWorkflow(self.runtime)
        self.number = 0

    def source(self, text: str) -> EvidenceSpan:
        self.number += 1
        event = Event(Source("worker", "session", "main", str(self.number)), "user", self.number,
                      "2026-10-30T10:00:00+08:00", "complete", text)
        receipt = self.runtime.submit(self.worker, event)
        return EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)

    def submit_run(self, drafts: tuple[ConsolidationDraft, ...]) -> Any:
        ids = tuple(dict.fromkeys(span.message_id for draft in drafts for span in draft.sources))
        lease = self.workflow.begin(self.worker, ids)
        assert lease.token is not None
        return self.workflow.complete(self.worker, lease.status.run_id, lease.token, drafts)

    def test_review_tier_default_proposes_without_merging(self) -> None:
        span = self.source("合成：用户说喜欢天文学")
        result = self.submit_run((ConsolidationDraft("profile_tag", "喜欢天文学", (span,), category="interest",
                                              confidence=0.8),))
        row = result.drafts[0]
        self.assertEqual(row.state, "pending_review")
        profile = ProfileRegistry(self.runtime).read(self.access, row.proposal_id)  # type: ignore[arg-type]
        self.assertEqual(profile.state, "draft")

    def test_auto_tier_merges_whitelisted_interest_tags_and_topics_only(self) -> None:
        self.workflow.configure(self.access, tier="auto")
        interest = self.source("合成：用户说喜欢观鸟")
        preference = self.source("合成：用户说偏好深色界面")
        topic = self.source("合成：本周聊了很多观鸟装备")
        result = self.submit_run((
            ConsolidationDraft("profile_tag", "喜欢观鸟", (interest,), category="interest", confidence=0.8),
            ConsolidationDraft("profile_tag", "偏好深色界面", (preference,), category="preference", confidence=0.8),
            ConsolidationDraft("topic", "观鸟装备话题", (topic,), confidence=0.6),
        ))
        states = {row.label: (row.state, row.proposal_id) for row in result.drafts}
        self.assertEqual(states["喜欢观鸟"][0], "merged")
        self.assertEqual(states["偏好深色界面"][0], "pending_review")
        self.assertEqual(states["观鸟装备话题"][0], "merged")
        merged = ProfileRegistry(self.runtime).read(self.access, states["喜欢观鸟"][1])  # type: ignore[arg-type]
        self.assertEqual(merged.state, "confirmed")
        self.assertIsNone(merged.record.confirmation_id)  # type: ignore[union-attr]
        still_draft = ProfileRegistry(self.runtime).read(self.access, states["偏好深色界面"][1])  # type: ignore[arg-type]
        self.assertEqual(still_draft.state, "draft")
        summary = ProfileRegistry(self.runtime).summary(self.access)
        self.assertEqual([tag.draft.label for tag in summary.tags], ["喜欢观鸟"])

    def test_manual_tier_proposes_nothing_and_whitelist_can_narrow(self) -> None:
        self.workflow.configure(self.access, tier="manual")
        span = self.source("合成：用户说喜欢种花")
        result = self.submit_run((ConsolidationDraft("profile_tag", "喜欢种花", (span,), category="interest",
                                              confidence=0.7),))
        self.assertIsNone(result.drafts[0].proposal_id)
        self.assertEqual(result.drafts[0].state, "pending_review")
        self.assertEqual(ProfileRegistry(self.runtime).summary(self.access).tags, ())
        self.workflow.configure(self.access, tier="auto", kinds=("topic",), profile_categories=())
        topic = self.source("合成：本周聊了花的养护")
        interest = self.source("合成：用户说喜欢兰花")
        result = self.submit_run((
            ConsolidationDraft("topic", "养花话题", (topic,), confidence=0.5),
            ConsolidationDraft("profile_tag", "喜欢兰花", (interest,), category="interest", confidence=0.8),
        ))
        states = {row.label: row.state for row in result.drafts}
        self.assertEqual(states["养花话题"], "merged")
        self.assertEqual(states["喜欢兰花"], "pending_review")

    def test_auto_confirm_is_hard_limited_to_interest_category(self) -> None:
        self.workflow.configure(self.access, tier="auto")
        preference = self.source("合成：用户说偏好猫")
        result = self.submit_run((ConsolidationDraft("profile_tag", "喜欢猫", (preference,), category="preference",
                                              confidence=0.9),))
        proposal_id = result.drafts[0].proposal_id
        assert proposal_id is not None
        with self.assertRaisesRegex(MemoryFailure, "profile_auto_confirm_limited"):
            self.workflow.profile.decide(self.access, ProfileDecision("tier-auto-direct", proposal_id, 1, "auto_confirm"))
        confirmation = self.runtime.submit(self.worker, Event(Source("worker", "session", "main", "confirm-pref"),
            "user", 999, None, "complete", "合成：确认偏好深色界面"))
        self.workflow.profile.decide(self.access, ProfileDecision("tier-manual-confirm", proposal_id, 1, "confirm",
            confirmation_id=confirmation.message_id))
        with self.assertRaisesRegex(MemoryFailure, "invalid_profile_transition"):
            self.workflow.profile.decide(self.access, ProfileDecision("tier-auto-after-confirm", proposal_id, 2,
                "auto_confirm"))

    def test_configure_validates_tier_whitelist_and_scope(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, "invalid_consolidation_tier"):
            self.workflow.configure(self.access, tier="full-auto")
        with self.assertRaisesRegex(MemoryFailure, "invalid_consolidation_whitelist"):
            self.workflow.configure(self.access, tier="auto", kinds=("fact",))
        with self.assertRaisesRegex(MemoryFailure, "invalid_consolidation_whitelist"):
            self.workflow.configure(self.access, tier="auto", profile_categories=("preference",))
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.workflow.configure(self.harness, tier="manual")
        self.assertEqual(self.workflow.configure(self.worker, tier="review")["tier"], "review")
        configured = self.workflow.configure(self.access, tier="auto", kinds=("navigation_summary",))
        self.assertEqual(configured, {"tier": "auto", "kinds": ["navigation_summary"],
                                      "profile_categories": ["interest"]})

    def test_cli_configure_end_to_end(self) -> None:
        process = subprocess.run([sys.executable, "-m", "evernook.cli", "consolidation-configure",
                                  "--config", str(self.config), "--access", str(self.owner_path)],
            input=json.dumps({"tier": "auto"}).encode("utf-8"), capture_output=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2])
        result = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result["result"]["tier"], "auto")
        self.assertEqual(result["result"]["profile_categories"], ["interest"])


if __name__ == "__main__":
    unittest.main()
