from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from evernook import CandidateWorkflow, Change, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize, pair_client
from evernook.context import ContextBuilder, ContextPolicy
from evernook.facts import FactDecision, FactRegistry
from evernook.preference_clarifications import OpenClarification, PreferenceClarifications, ResolveClarification
from evernook.preferences import PreferenceProcessor
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry
from evernook.purge import PurgeDecision, PurgeWorkflow
from test_context import SyntheticCounter
from test_preferences import WholeSourceSelector
from test_semantic import SyntheticEmbedding


class PreferenceClarificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-preference-clarifications-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="synthetic")
        self.runtime = MemoryRuntime(self.config)
        self.facts, self.profiles = FactRegistry(self.runtime), ProfileRegistry(self.runtime)
        self.number = 0
        old_id = self.candidate("我偏好简短回答。")
        old_candidate, = CandidateWorkflow(self.runtime).candidates(self.access, self.old_run)
        self.old = self.facts.decide(self.access, FactDecision("old-fact", "self", "answer_style", old_id,
            self.save("合成：确认此前的简短偏好。"), 0, "assert", valid_from="2021-01-01T00:00:00+08:00"))
        self.profiles.decide(self.access, ProfileDecision("old-profile", "answer-style", 0, "propose",
            ProfileDraft("preference", "我偏好简短回答。", 1.0, old_candidate.draft.sources)))
        self.profile = self.profiles.decide(self.access, ProfileDecision("confirm-profile", "answer-style", 1, "confirm",
            confirmation_id=self.save("合成：确认简短偏好画像。")))
        self.incoming = self.candidate("我偏好详细回答。")
        self.workflow = PreferenceClarifications(self.runtime)
        self.open_request = OpenClarification("self", "answer_style", "answer-style", self.incoming,
            self.save("合成：两条偏好冲突，请保留双方并请求澄清。"), 1, 2)

    def save(self, text: str) -> str:
        self.number += 1
        return self.runtime.submit(self.access, Event(Source("synthetic", "session", "main", str(self.number)),
            "user", self.number, "2026-11-04T00:00:00+08:00", "complete", text)).message_id

    def candidate(self, text: str) -> str:
        message_id = self.save(text)
        report = PreferenceProcessor(self.runtime, WholeSourceSelector()).process(self.access, (message_id,))
        self.old_run = report.run.run_id
        candidate, = CandidateWorkflow(self.runtime).candidates(self.access, report.run.run_id)
        return candidate.candidate_id

    def context(self) -> dict[str, Any]:
        policy = ContextPolicy(30000, 30000, 0, 7, 8192, fact_keys=(("self", "answer_style"),), profile_tokens=3000)
        bundle = ContextBuilder(self.runtime, SemanticRecall(self.runtime), policy, SyntheticCounter()).build(
            self.access, "", as_of="2026-11-04T01:00:00+08:00", mode="normal", timeout=30)
        result: dict[str, Any] = json.loads(bundle.payload)
        return result

    def test_one_durable_clarification_hides_confirmed_profile_and_withholds_certain_fact(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        repeated = PreferenceClarifications(MemoryRuntime(self.config)).open(self.access, self.open_request)
        self.assertEqual(repeated, opened)
        self.assertEqual(opened.status, "pending")
        view = self.facts.query(self.access, "self", "answer_style")
        self.assertEqual(view.fact, self.old)
        self.assertTrue(view.clarification_required)
        self.assertEqual([item.fact_id for item in view.disputes], [opened.clarification_id])
        self.assertEqual(self.profiles.summary(self.access).tags, ())
        self.assertEqual(self.profiles.read(self.access, "answer-style").state, "draft")
        context = self.context()
        self.assertEqual(context["kind"], "untrusted_memory_context")
        self.assertFalse(any(item["kind"] == "profile" for item in context["items"]))
        facts, = [item for item in context["items"] if item["kind"] == "facts"]
        self.assertIsNone(facts["data"]["fact"])
        self.assertTrue(facts["data"]["clarification_required"])

    def test_explicit_correction_resolves_once_and_preserves_historical_knowledge(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        request = ResolveClarification("self", "answer_style", "answer-style", opened.clarification_id,
            self.save("合成：以前说错了，我确认长期偏好详细回答。"), 2, 3, "correction", "incoming")
        resolved = self.workflow.resolve(self.access, request)
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.fact.kind, "correction")
        self.assertEqual(resolved.fact.valid_from, self.old.valid_from)
        self.assertEqual(resolved.fact.replaces_id, self.old.fact_id)
        self.assertEqual(resolved.fact.resolves, (opened.clarification_id,))
        self.assertEqual(self.workflow.resolve(self.access, request), resolved)
        current = self.facts.query(self.access, "self", "answer_style")
        self.assertFalse(current.clarification_required)
        self.assertEqual(current.fact, resolved.fact)
        self.assertEqual(self.profiles.summary(self.access).tags, (resolved.profile,))
        before = self.facts.query(self.access, "self", "answer_style", known_at=self.old.recorded_at)
        self.assertEqual(before.fact, self.old)
        with self.assertRaisesRegex(MemoryFailure, "fact_request_conflict"):
            self.workflow.resolve(self.access, replace(request, kind="change", valid_from="2024-01-01T00:00:00+08:00"))

    def test_suppressing_unresolved_evidence_cannot_restore_old_certainty_or_publish_old_resolution(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        request = ResolveClarification("self", "answer_style", "answer-style", opened.clarification_id,
            self.save("合成：确认新的详细偏好。"), 2, 3, "correction", "incoming")
        source = opened.fact.sources[0].message_id
        self.runtime.change(self.access, Change("suppress-conflicting-source", source, 1, "suppress",
            self.save("合成：暂停使用争议新来源。"), "synthetic"))
        view = self.facts.query(self.access, "self", "answer_style")
        self.assertTrue(view.clarification_required)
        self.assertEqual(view.disputes, ())
        self.assertEqual(self.profiles.summary(self.access).tags, ())
        for item in self.context()["items"]:
            if item["kind"] == "facts":
                self.assertIsNone(item["data"]["fact"])
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            self.workflow.resolve(self.access, request)

    def test_conflict_opened_during_context_build_removes_the_captured_certain_fact(self) -> None:
        backend = SyntheticEmbedding()
        recall = SemanticRecall(self.runtime, backend)
        recall.refresh(self.access)
        backend.hook = lambda: self.workflow.open(self.access, self.open_request)
        policy = ContextPolicy(30000, 30000, 0, 7, 8192, fact_keys=(("self", "answer_style"),), profile_tokens=3000)
        bundle = ContextBuilder(self.runtime, recall, policy, SyntheticCounter()).build(self.access,
            "合成：本轮请详细回答", as_of="2026-11-04T01:00:00+08:00", mode="normal", timeout=30)
        result = json.loads(bundle.payload)
        self.assertEqual(self.profiles.read(self.access, "answer-style").state, "draft")
        self.assertFalse(any(item["kind"] in {"profile", "facts"} for item in result["items"]))

    def test_profile_cas_failure_rolls_back_both_open_and_resolution(self) -> None:
        with self.assertRaisesRegex(MemoryFailure, "profile_revision_conflict"):
            self.workflow.open(self.access, replace(self.open_request, expected_profile_revision=0))
        self.assertEqual(self.facts.query(self.access, "self", "answer_style").revision, 1)
        self.assertEqual(self.profiles.read(self.access, "answer-style").state, "confirmed")
        opened = self.workflow.open(self.access, self.open_request)
        request = ResolveClarification("self", "answer_style", "answer-style", opened.clarification_id,
            self.save("合成：确认详细偏好。"), 2, 0, "correction", "incoming")
        with self.assertRaisesRegex(MemoryFailure, "profile_revision_conflict"):
            self.workflow.resolve(self.access, request)
        self.assertEqual(self.facts.query(self.access, "self", "answer_style").revision, 2)
        self.assertEqual(self.profiles.read(self.access, "answer-style").state, "draft")

    def test_concurrent_duplicate_open_reuses_one_pending_identity(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.workflow.open(self.access, self.open_request), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.facts.query(self.access, "self", "answer_style").revision, 2)
        self.assertEqual(self.profiles.read(self.access, "answer-style").revision, 3)

    def test_purged_dispute_keeps_pending_metadata_and_blocks_old_resolution(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        request = ResolveClarification("self", "answer_style", "answer-style", opened.clarification_id,
            self.save("合成：确认新的详细偏好。"), 2, 3, "correction", "incoming")
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, "purge-dispute", (opened.fact.sources[0].message_id,))
        purge.purge(self.access, PurgeDecision(preview.request_id, self.save("合成：确认彻底删除刚才预览的争议来源。")))
        current = self.facts.query(self.access, "self", "answer_style")
        self.assertTrue(current.clarification_required)
        self.assertEqual(current.disputes, ())
        self.assertEqual(self.profiles.summary(self.access).tags, ())
        with self.assertRaisesRegex(MemoryFailure, "preference_source_unavailable"):
            self.workflow.resolve(self.access, request)

    def test_real_cli_change_confirmation_preserves_two_effective_periods(self) -> None:
        access_file = self.config.parent / "mcp-access-cli.json"
        pair_client(self.config, client_id="synthetic", entry="synthetic", access_path=access_file)
        def invoke(command: str, payload: object) -> dict[str, Any]:
            process = subprocess.run([sys.executable, "-m", "evernook.cli", command, "--config", str(self.config),
                "--access", str(access_file)], input=json.dumps(payload).encode(), capture_output=True, timeout=30)
            self.assertEqual(process.stderr, b"")
            self.assertNotIn(self.access.token.encode(), process.stdout)
            result: dict[str, Any] = json.loads(process.stdout)
            return result
        opened = invoke("preference-clarify", asdict(self.open_request))
        self.assertTrue(opened["ok"], opened)
        request = ResolveClarification("self", "answer_style", "answer-style", opened["result"]["clarification_id"],
            self.save("合成：我确认2024年起现实偏好改为详细回答。"), 2, 3, "change", "incoming", "2024-01-01T00:00:00+08:00")
        invalid = asdict(request)
        invalid["kind"] = []
        self.assertEqual(invoke("preference-resolve", invalid)["error"]["code"], "invalid_preference_resolution")
        resolved = invoke("preference-resolve", asdict(request))
        self.assertTrue(resolved["ok"], resolved)
        past = self.facts.query(self.access, "self", "answer_style", valid_at="2023-01-01T00:00:00+08:00")
        current = self.facts.query(self.access, "self", "answer_style", valid_at="2025-01-01T00:00:00+08:00")
        self.assertEqual(past.fact, self.old)
        self.assertEqual(past.effective_until, request.valid_from)
        assert current.fact is not None
        self.assertEqual(current.fact.text, "我偏好详细回答。")
        self.assertEqual(current.fact.kind, "change")
        self.assertEqual(len(self.profiles.history(self.access, "answer-style").records), 4)

    def test_user_can_explicitly_retain_existing_preference_in_one_clarification(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        request = ResolveClarification("self", "answer_style", "answer-style", opened.clarification_id,
            self.save("合成：原来的简短偏好正确，新说法无效，我确认保留原偏好。"), 2, 3, "correction", choice="existing")
        resolved = self.workflow.resolve(self.access, request)
        self.assertEqual(resolved.fact.text, "我偏好简短回答。")
        self.assertEqual(resolved.profile.draft.label, "我偏好简短回答。")
        self.assertEqual(resolved.fact.resolves, (opened.clarification_id,))
        self.assertFalse(self.facts.query(self.access, "self", "answer_style").clarification_required)
        self.assertEqual(self.profiles.summary(self.access).tags, (resolved.profile,))
        with self.assertRaisesRegex(MemoryFailure, "preference_clarification_resolved"):
            self.workflow.open(self.access, self.open_request)

    def test_direct_profile_confirmation_and_revision_cannot_bypass_pending_clarification(self) -> None:
        self.workflow.open(self.access, self.open_request)
        confirmation = self.save("合成：直接确认画像不能绕过事实争议。")
        with self.assertRaisesRegex(MemoryFailure, "preference_clarification_required"):
            self.profiles.decide(self.access, ProfileDecision("bypass-confirm", "answer-style", 3, "confirm",
                confirmation_id=confirmation))
        previous = self.profiles.read(self.access, "answer-style").record
        assert previous is not None
        with self.assertRaisesRegex(MemoryFailure, "preference_clarification_required"):
            self.profiles.decide(self.access, ProfileDecision("bypass-revise", "answer-style", 3, "revise",
                replace(previous.draft, processor_version="trusted-proposal-v1")))
        with self.assertRaisesRegex(MemoryFailure, "preference_clarification_required"):
            self.profiles.decide(self.access, ProfileDecision("bypass-reject", "answer-style", 3, "reject",
                confirmation_id=confirmation))
        self.assertEqual(self.profiles.summary(self.access).tags, ())

    def test_pending_interest_cannot_use_existing_auto_confirm_path(self) -> None:
        old_id = self.candidate("我喜欢观鸟。")
        old, = CandidateWorkflow(self.runtime).candidates(self.access, self.old_run)
        self.facts.decide(self.access, FactDecision("hobby-fact", "self", "hobby", old_id,
            self.save("合成：确认旧观鸟兴趣。"), 0, "assert", valid_from="2021-01-01T00:00:00+08:00"))
        self.profiles.decide(self.access, ProfileDecision("hobby-profile", "hobby", 0, "propose",
            ProfileDraft("interest", old.draft.text, 1.0, old.draft.sources)))
        self.profiles.decide(self.access, ProfileDecision("hobby-confirm", "hobby", 1, "confirm",
            confirmation_id=self.save("合成：确认观鸟画像。")))
        new_id = self.candidate("我喜欢摄影。")
        self.workflow.open(self.access, OpenClarification("self", "hobby", "hobby", new_id,
            self.save("合成：本测试只将一项主兴趣作为互斥槽，请求澄清。"), 1, 2))
        with self.assertRaisesRegex(MemoryFailure, "preference_clarification_required"):
            self.profiles.decide(self.access, ProfileDecision("bypass-auto", "hobby", 3, "auto_confirm"))
        self.assertEqual(self.profiles.read(self.access, "hobby").state, "draft")

    def test_unreadable_sides_still_emit_metadata_for_the_pending_clarification(self) -> None:
        opened = self.workflow.open(self.access, self.open_request)
        sources = (self.old.sources[0].message_id, opened.fact.sources[0].message_id)
        control = self.save("合成：暂停争议双方来源。")
        for index, source in enumerate(sources):
            self.runtime.change(self.access, Change("unreadable-" + str(index), source, 1, "suppress", control, "synthetic"))
        current = self.facts.query(self.access, "self", "answer_style")
        self.assertTrue(current.clarification_required)
        self.assertIsNone(current.fact)
        facts, = [item for item in self.context()["items"] if item["kind"] == "facts"]
        self.assertTrue(facts["data"]["clarification_required"])
        self.assertIn(opened.clarification_id, facts["data"]["unavailable_ids"])
        self.assertIsNone(facts["data"]["fact"])
        self.assertEqual(facts["data"]["disputes"], [])


if __name__ == "__main__":
    unittest.main()
