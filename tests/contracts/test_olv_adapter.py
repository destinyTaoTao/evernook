from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from typing import Any

from evernook import HARNESS_DEFAULT_SCOPES, MemoryRuntime, initialize
from evernook.adapters import ConformanceSuite, OLVReferenceAdapter
from evernook.adapters.surface import AdapterGap, AdapterSurface
from evernook.context import ContextBundle, ContextPolicy
from evernook.profile import ProfileRegistry


class OLVAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-olv-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / "ordinary", client_id="owner", entry="owner")

    def suite(self, adapter: AdapterSurface) -> ConformanceSuite:
        return ConformanceSuite(adapter, profile_runtime=MemoryRuntime(self.config), profile_access=self.owner)

    def test_reference_adapter_passes_the_full_conformance_suite(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        access = adapter.pair("olv", "olv")
        self.assertEqual(sorted(HARNESS_DEFAULT_SCOPES), sorted(self.runtime_scopes(access)))
        report = self.suite(adapter).run()
        for result in report.results:
            self.assertTrue(result.passed, f"{result.name}: {result.detail}")
        self.assertTrue(report.passed)
        kinds = {gap.event_kind for gap in adapter.gaps()}
        self.assertIn("voice_asr_transcript", kinds)
        self.assertIn("vision_frame", kinds)

    def runtime_scopes(self, access: Any) -> list[str]:
        runtime = MemoryRuntime(self.config)
        return sorted(runtime._authorize(access).scopes)

    def test_suite_fails_a_nonconforming_adapter(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        adapter.pair("olv-broken", "olv-broken")

        class BrokenAdapter(AdapterSurface):
            name = "broken-reference"
            def __init__(self, inner: OLVReferenceAdapter) -> None:
                self.inner = inner
            def pair(self, client_id: str, entry: str) -> Any:
                return self.inner.pair(client_id, entry)
            def covered_events(self) -> tuple[str, ...]:
                return self.inner.covered_events()
            def gaps(self) -> tuple[AdapterGap, ...]:
                return ()  # pretends full coverage
            def capture(self, event: Any) -> Any:
                return self.inner.capture(event)
            def read_back(self, message_id: str) -> Any:
                return self.inner.read_back(message_id)
            def inject(self, query: str, policy: Any, counter: Any) -> Any:
                return self.inner.inject(query, policy, counter)
            def attempt_out_of_scope(self) -> str:
                return self.inner.attempt_out_of_scope()
            def access(self) -> Any:
                return self.inner.access()

        report = self.suite(BrokenAdapter(adapter)).run()
        failing = {result.name for result in report.results if not result.passed}
        self.assertIn("gap_disclosure_declares_uncovered", failing)
        self.assertFalse(report.passed)

    def test_conformance_report_is_json_serializable(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        adapter.pair("olv-report", "olv-report")
        report = self.suite(adapter).run()
        payload = json.dumps({"adapter": report.adapter, "passed": report.passed,
                              "results": [{"name": item.name, "passed": item.passed, "detail": item.detail}
                                          for item in report.results]}, ensure_ascii=False)
        self.assertIn("capture_roundtrip_original", payload)

    def test_suite_rejects_zero_token_claim_and_self_expanded_requested_budget(self) -> None:
        for lie in ("zero", "expanded"):
            with self.subTest(lie=lie):
                class BudgetLiar(OLVReferenceAdapter):
                    def inject(self, query: str, policy: ContextPolicy, counter: object) -> ContextBundle:
                        if lie == "expanded":
                            return super().inject(query, replace(policy, normal_tokens=4096, deep_tokens=4096), counter)
                        return replace(super().inject(query, policy, counter),
                            payload='{"kind":"untrusted_memory_context","items":[]}', used_tokens=0, evidence_count=0)
                adapter = BudgetLiar(self.config)
                adapter.pair("liar", "liar")
                report = self.suite(adapter).run()
                check = next(result for result in report.results if result.name == "injection_holds_budget_and_reports_omissions")
                self.assertFalse(check.passed)

    def test_suite_rejects_suppressed_profile_identity_content_and_escaped_content_leaks(self) -> None:
        for leak in ("identity", "content", "escaped", "shadowed", "shadowed_escaped"):
            with self.subTest(leak=leak):
                class ProfileLeaker(OLVReferenceAdapter):
                    def inject(self, query: str, policy: ContextPolicy, counter: object) -> ContextBundle:
                        bundle = super().inject(query, policy, counter)
                        if not bundle.payload:
                            return bundle
                        payload = json.loads(bundle.payload)
                        shadowed: list[str] = []
                        for tag in ProfileRegistry(self.runtime).summary(self.access(), include_suppressed=True).tags:
                            if tag.state != "suppressed":
                                continue
                            if leak.startswith("shadowed"):
                                shadowed.extend((tag.tag_id, tag.draft.label))
                                continue
                            payload["items"].append({"kind": "profile",
                                "evidence_id": "profile:" + tag.tag_id if leak == "identity" else "apparently-valid-id",
                                "data": {"label": "removed" if leak == "identity" else tag.draft.label}})
                        raw = json.dumps(payload, ensure_ascii=False)
                        if leak == "escaped":
                            raw = raw.replace("synthetic-profile-marker", "\\u0073ynthetic-profile-marker")
                        if shadowed:
                            raw = raw[:-1] + ',"leak":' + json.dumps(" ".join(shadowed)) + ',"leak":""}'
                        if leak == "shadowed_escaped":
                            raw = raw.replace("synthetic-profile-marker", "\\u0073ynthetic-profile-marker")
                        return replace(bundle, payload=raw, used_tokens=len(raw))
                adapter = ProfileLeaker(self.config)
                adapter.pair("leaker", "leaker")
                report = self.suite(adapter).run()
                check = next(result for result in report.results if result.name == "injection_excludes_suppressed_profile_tags")
                self.assertFalse(check.passed)

    def test_missing_trusted_profile_fixture_cannot_claim_profile_conformance(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        adapter.pair("no-fixture", "no-fixture")
        report = ConformanceSuite(adapter).run()
        self.assertFalse(report.passed)
        self.assertEqual(report.counter_scope, "synthetic_contract_only")
        self.assertIn("codepoint", report.counter_id)
        self.assertFalse(next(result for result in report.results
            if result.name == "injection_excludes_suppressed_profile_tags").passed)


if __name__ == "__main__":
    unittest.main()
