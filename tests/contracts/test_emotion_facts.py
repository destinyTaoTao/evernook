from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any

from evernook import (CandidateDraft, CandidateWorkflow, Change, Event, FACT_CATEGORIES, EvidenceSpan, MemoryFailure,
                      MemoryRuntime, Source, initialize, pair_client)
from evernook.facts import FactDecision, FactRegistry
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry


class EmotionFactTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-emotion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.owner_path = self.config.parent / "mcp-access-owner.json"
        pair_client(self.config, client_id="synthetic", entry="synthetic", access_path=self.owner_path)
        self.facts = FactRegistry(self.runtime)
        self.profiles = ProfileRegistry(self.runtime)
        self.workflow = CandidateWorkflow(self.runtime)
        self.number = 0

    def candidate(self, text: str) -> tuple[str, str]:
        self.number += 1
        event = Event(Source("synthetic", "session", "main", str(self.number)), "user", self.number,
                      "2025-01-01T12:00:00+08:00", "complete", text)
        receipt = self.runtime.submit(self.access, event)
        lease = self.workflow.begin(self.access, (receipt.message_id,), processor_version="synthetic-explicit-v1")
        assert lease.token is not None
        span = EvidenceSpan(receipt.message_id, 1, "original", 1, 0, len(text), text)
        self.workflow.complete(self.access, lease.status.run_id, lease.token, (CandidateDraft(text, "user_statement", (span,)),))
        candidate = self.workflow.candidates(self.access, lease.status.run_id)[0]
        return candidate.candidate_id, receipt.message_id

    def decide(self, request_id: str, text: str, *, predicate: str, category: str | None = None, kind: str = "assert",
               valid_from: str | None = None, replaces: Any = None, expected: int | None = None) -> Any:
        candidate, evidence = self.candidate(text)
        return self.facts.decide(self.access, FactDecision(request_id, "synthetic-person", predicate,
            candidate, evidence, (0 if kind == "assert" else 1) if expected is None else expected, kind, valid_from,
            replaces_id=replaces.fact_id if replaces else None, category=category))

    def test_emotion_categories_ride_ordinary_fact_machinery(self) -> None:
        day = self.decide("synthetic-day", "合成：我们的纪念日是5月20日", predicate="important_day",
                          category="important_day")
        self.assertEqual(day.category, "important_day")
        mood = self.decide("synthetic-mood", "合成：用户今天心情很好", predicate="mood", category="mood")
        milestone = self.decide("synthetic-milestone", "合成：认识满一年的里程碑", predicate="milestone",
                                category="milestone", valid_from="2026-10-30T00:00:00+08:00")
        self.assertEqual({day.category, mood.category, milestone.category}, set(FACT_CATEGORIES))
        view = self.facts.query(self.access, "synthetic-person", "milestone")
        self.assertEqual(view.fact, milestone)
        self.assertEqual(view.fact.category, "milestone")  # type: ignore[union-attr]
        past = self.facts.query(self.access, "synthetic-person", "milestone", valid_at="2025-06-01T00:00:00+08:00")
        self.assertEqual(past.fact, None)
        with self.assertRaisesRegex(MemoryFailure, "invalid_fact_category"):
            self.decide("synthetic-bad", "合成：天气类别不成立", predicate="weather", category="weather")

    def test_correction_change_and_suppression_propagate_like_ordinary_facts(self) -> None:
        first = self.decide("synthetic-mood-1", "合成：用户本周心情低落", predicate="mood", category="mood")
        corrected = self.decide("synthetic-mood-2", "合成：更正，用户本周心情平稳", predicate="mood",
                                category="mood", kind="correction", replaces=first)
        self.assertEqual(corrected.category, "mood")
        view = self.facts.query(self.access, first.subject_id, first.predicate)
        self.assertEqual(view.fact, corrected)
        _, control = self.candidate("合成：抑制控制消息")
        self.runtime.change(self.access, Change("suppress-mood", corrected.sources[0].message_id, 1, "suppress",
                                                control, "合成"))
        suppressed_view = self.facts.query(self.access, first.subject_id, first.predicate)
        self.assertEqual(suppressed_view.state, "source_unavailable")
        self.assertEqual(suppressed_view.fact, None)
        self.assertEqual(suppressed_view.unavailable_ids, (corrected.fact_id,))

    def test_mood_and_milestone_tags_flow_the_profile_taxonomy(self) -> None:
        registry = ProfileRegistry(self.runtime)
        for tag_id, label, category in (("tag-day", "5月20日纪念日", "important_day"),
                                        ("tag-milestone", "认识满一年", "milestone"),
                                        ("tag-mood", "本周情绪平稳", "mood")):
            span_text = f"合成：用户提到{label}"
            self.number += 1
            event = Event(Source("synthetic", "session", "main", str(self.number)), "user", self.number, None,
                          "complete", span_text)
            receipt = self.runtime.submit(self.access, event)
            registry.decide(self.access, ProfileDecision(f"propose-{tag_id}", tag_id, 0, "propose",
                ProfileDraft(category, label, 0.6, (EvidenceSpan(receipt.message_id, 1, "original", 1, 0,
                    len(span_text), span_text),))))
            confirmation = self.runtime.submit(self.access, Event(Source("synthetic", "session", "main",
                f"confirm-{tag_id}"), "user", 1000 + self.number, None, "complete", f"合成：确认{label}"))
            registry.decide(self.access, ProfileDecision(f"confirm-{tag_id}", tag_id, 1, "confirm",
                confirmation_id=confirmation.message_id))
        summary = registry.summary(self.access)
        self.assertEqual(len(summary.tags), 3)
        self.assertEqual(summary.taxonomy_version, 1)
        by_category = registry.summary(self.access, categories=("mood",))
        self.assertEqual([tag.draft.category for tag in by_category.tags], ["mood"])

    def test_cli_fact_decide_carries_category(self) -> None:
        candidate_id, evidence = self.candidate("合成：CLI 纪念日是6月1日")
        payload = {"request_id": "cli-emotion", "subject_id": "synthetic-person", "predicate": "important_day",
                   "candidate_id": candidate_id, "confirmation_id": evidence, "expected_revision": 0,
                   "kind": "assert", "category": "important_day"}
        process = subprocess.run([sys.executable, "-m", "evernook.cli", "fact-decide", "--config", str(self.config),
                                  "--access", str(self.owner_path)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2])
        result = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result["result"]["category"], "important_day")


if __name__ == "__main__":
    unittest.main()
