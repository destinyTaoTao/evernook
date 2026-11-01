from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from evernook import Event, EvidenceSpan, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.context import ContextBuilder, ContextPolicy
from evernook.persona import PersonaRegistry, PersonaSet
from evernook.profile import ProfileDecision, ProfileDraft, ProfileRegistry
from test_context import SyntheticCounter


class CompanionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-context-companion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.access = initialize(self.root / "ordinary", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.recall = SemanticRecall(self.runtime)
        self.counter = SyntheticCounter()
        self.policy = ContextPolicy(4000, 30000, 1500, 7, 8192, persona_tokens=600, profile_tokens=400)
        self.as_of = "2026-10-30T18:00:00+08:00"
        self.number = 0

    def save(self, text: str) -> str:
        self.number += 1
        return self.runtime.submit(self.access, Event(Source("test", "session", "main", str(self.number)), "user",
            self.number, "2026-10-01T12:00:00+08:00", "complete", text)).message_id

    def build(self, query: str, policy: ContextPolicy | None = None) -> Any:
        return ContextBuilder(self.runtime, self.recall, policy or self.policy, self.counter).build(
            self.access, query, as_of=self.as_of, mode="normal", timeout=20)

    def persona(self, block: str, text: str, revision: int, request_id: str) -> Any:
        return PersonaRegistry(self.runtime).set(self.access, PersonaSet(request_id, block, text,
            self.save(f"合成：请把{block}设为{text}"), "合成：用户确认", revision))

    def confirmed_tag(self, tag_id: str, label: str, confidence: float, *, confirmed: bool = True) -> None:
        registry = ProfileRegistry(self.runtime)
        span = self.save(f"合成：用户提到{label}")
        registry.decide(self.access, ProfileDecision(f"propose-{tag_id}", tag_id, 0, "propose",
            ProfileDraft("interest", label, confidence, (EvidenceSpan(span, 1, "original", 1, 0,
                len(f"合成：用户提到{label}"), f"合成：用户提到{label}"),))))
        if confirmed:
            confirmation = self.save(f"合成：确认{label}")
            registry.decide(self.access, ProfileDecision(f"confirm-{tag_id}", tag_id, 1, "confirm",
                confirmation_id=confirmation))

    def kinds(self, bundle: Any) -> list[str]:
        return [item["kind"] for item in json.loads(bundle.payload)["items"]]

    def ids(self, bundle: Any) -> list[str]:
        return [item["evidence_id"] for item in json.loads(bundle.payload)["items"]]

    def test_persona_blocks_and_profile_summary_injected_with_independent_budgets(self) -> None:
        self.persona("tone", "温和，爱用句号。", 0, "ctx-tone")
        self.persona("address", "称呼：小合成", 0, "ctx-address")
        self.confirmed_tag("ctx-tag-a", "喜欢摄影", 0.9)
        self.confirmed_tag("ctx-tag-draft", "未确认标签", 0.95, confirmed=False)
        bundle = self.build("合成")
        kinds = self.kinds(bundle)
        self.assertIn("persona", kinds)
        self.assertIn("profile", kinds)
        payload = json.loads(bundle.payload)
        persona_items = [item for item in payload["items"] if item["kind"] == "persona"]
        profile_items = [item for item in payload["items"] if item["kind"] == "profile"]
        self.assertEqual({item["evidence_id"] for item in persona_items}, {"persona:tone", "persona:address"})
        self.assertEqual([item["data"]["label"] for item in profile_items], ["喜欢摄影"])
        self.assertLessEqual(bundle.used_tokens, bundle.token_limit)
        zero = self.build("合成", replace(self.policy, persona_tokens=0, profile_tokens=0))
        kinds_zero = self.kinds(zero)
        self.assertNotIn("persona", kinds_zero)
        self.assertNotIn("profile", kinds_zero)

    def test_budget_overflow_omits_lowest_priority_items_with_explicit_reasons(self) -> None:
        self.persona("tone", "合" * 400, 0, "ctx-big-tone")
        self.persona("address", "称呼：小合成", 0, "ctx-address")
        self.confirmed_tag("ctx-tag-high", "喜欢摄影", 0.9)
        self.confirmed_tag("ctx-tag-low", "周末爬山", 0.3)
        bundle = self.build("合成", replace(self.policy, persona_tokens=500, profile_tokens=200))
        omitted = {item.evidence_id: item.reason for item in bundle.omitted}
        self.assertEqual(omitted.get("persona:address"), "persona_budget")
        self.assertEqual(omitted.get("profile:ctx-tag-low"), "profile_budget")
        payload = json.loads(bundle.payload)
        persona_ids = {item["evidence_id"] for item in payload["items"] if item["kind"] == "persona"}
        profile_ids = {item["evidence_id"] for item in payload["items"] if item["kind"] == "profile"}
        self.assertEqual(persona_ids, {"persona:tone"})
        self.assertEqual(profile_ids, {"profile:ctx-tag-high"})
        self.assertFalse(bundle.complete)

    def test_suppressed_profile_tag_leaves_injection_and_persona_rollback_updates_it(self) -> None:
        from evernook.persona import PersonaRollback
        self.confirmed_tag("ctx-tag-gone", "喜欢摄影", 0.8)
        first_version = self.persona("tone", "活泼！", 0, "ctx-tone-v1")
        first = self.build("合成")
        self.assertIn("profile:ctx-tag-gone", self.ids(first))
        confirmation = self.save("合成：这条标签不对，抑制")
        ProfileRegistry(self.runtime).decide(self.access, ProfileDecision("ctx-suppress", "ctx-tag-gone", 2,
            "suppress", confirmation_id=confirmation))
        second = self.build("合成")
        self.assertNotIn("profile:ctx-tag-gone", self.ids(second))
        self.persona("tone", "安静。", 1, "ctx-tone-v2")
        third = self.build("合成")
        tone_payloads = [item["data"]["text"] for item in json.loads(third.payload)["items"]
                         if item["kind"] == "persona" and item["evidence_id"] == "persona:tone"]
        self.assertEqual(tone_payloads, ["安静。"])
        view = PersonaRegistry(self.runtime).view(self.access)
        PersonaRegistry(self.runtime).rollback(self.access, PersonaRollback("ctx-rollback", first_version.seq,
            view.head_seq, self.save("合成：撤销第二版语气"), "合成"))
        fourth = self.build("合成")
        tone_payloads = [item["data"]["text"] for item in json.loads(fourth.payload)["items"]
                         if item["kind"] == "persona" and item["evidence_id"] == "persona:tone"]
        self.assertEqual(tone_payloads, ["活泼！"])

    def test_profile_suppressed_after_summary_is_removed_from_final_context(self) -> None:
        registry = ProfileRegistry(self.runtime)
        self.confirmed_tag("ctx-interleaved", "合成唯一画像标记", 0.8)
        changed = False
        outer = self
        class InterleavingCounter(SyntheticCounter):
            def count(self, text: str) -> int:
                nonlocal changed
                if not changed and '"tag_id"' in text and "ctx-interleaved" in text:
                    changed = True
                    registry.decide(outer.access, ProfileDecision("ctx-interleaved-suppress", "ctx-interleaved", 2,
                        "suppress", confirmation_id=outer.save("合成：抑制刚才的画像")))
                return len(text)
        self.counter = InterleavingCounter()
        bundle = self.build("")
        self.assertTrue(changed)
        self.assertNotIn("profile:ctx-interleaved", self.ids(bundle))
        self.assertNotIn("合成唯一画像标记", bundle.payload)
        self.assertTrue(any(item.evidence_id == "profile:ctx-interleaved"
            and item.reason == "source_changed_during_context" for item in bundle.omitted))

    def test_revised_purged_or_unconfirmed_profile_snapshot_is_not_delivered(self) -> None:
        from evernook import Change
        from evernook.purge import PurgeDecision, PurgeWorkflow

        registry = ProfileRegistry(self.runtime)
        for action in ("revise", "purge", "confirmation"):
            with self.subTest(action=action):
                tag_id = "ctx-race-" + action
                label = "合成旧画像唯一标记" + action
                self.confirmed_tag(tag_id, label, 0.8)
                original = registry.read(self.access, tag_id).record
                assert original is not None and original.confirmation_id is not None
                changed = False
                outer = self
                class InterleavingCounter(SyntheticCounter):
                    def count(self, text: str) -> int:
                        nonlocal changed
                        assert original is not None
                        if not changed and '"tag_id"' in text and tag_id in text:
                            changed = True
                            if action == "revise":
                                updated_text = "合成：更正后的画像来源"
                                source = outer.save(updated_text)
                                registry.decide(outer.access, ProfileDecision("revise-" + tag_id, tag_id, 2, "revise",
                                    ProfileDraft("interest", "合成新画像", 0.8,
                                        (EvidenceSpan(source, 1, "original", 1, 0, len(updated_text), updated_text),))))
                                registry.decide(outer.access, ProfileDecision("reconfirm-" + tag_id, tag_id, 3, "confirm",
                                    confirmation_id=outer.save("合成：确认更正后的画像")))
                            elif action == "purge":
                                purge = PurgeWorkflow(outer.runtime)
                                preview = purge.preview(outer.access, "purge-" + tag_id, (original.draft.sources[0].message_id,))
                                purge.purge(outer.access, PurgeDecision(preview.request_id,
                                    outer.save("合成：确认删除预览中的画像来源")))
                            else:
                                assert original.confirmation_id is not None and original.confirmation_version is not None
                                outer.runtime.change(outer.access, Change("suppress-" + tag_id, original.confirmation_id,
                                    original.confirmation_version, "suppress", outer.save("合成：抑制画像确认"), "synthetic"))
                        return len(text)
                self.counter = InterleavingCounter()
                bundle = self.build("", replace(self.policy, profile_tokens=2000))
                self.assertTrue(changed)
                self.assertNotIn("profile:" + tag_id, self.ids(bundle))
                self.assertNotIn(label, bundle.payload)
                self.assertTrue(any(item.evidence_id == "profile:" + tag_id
                    and item.reason == "source_changed_during_context" for item in bundle.omitted))


if __name__ == "__main__":
    unittest.main()
