from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from evernook import HARNESS_DEFAULT_SCOPES, MemoryRuntime, initialize
from evernook.adapters import ConformanceSuite, OLVReferenceAdapter
from evernook.adapters.surface import AdapterGap, AdapterSurface


class OLVAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-olv-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, _ = initialize(self.root / "ordinary", client_id="owner", entry="owner")

    def test_reference_adapter_passes_the_full_conformance_suite(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        access = adapter.pair("olv", "olv")
        self.assertEqual(sorted(HARNESS_DEFAULT_SCOPES), sorted(self.runtime_scopes(access)))
        report = ConformanceSuite(adapter).run()
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

        report = ConformanceSuite(BrokenAdapter(adapter)).run()
        failing = {result.name for result in report.results if not result.passed}
        self.assertIn("gap_disclosure_declares_uncovered", failing)
        self.assertFalse(report.passed)

    def test_conformance_report_is_json_serializable(self) -> None:
        adapter = OLVReferenceAdapter(self.config)
        adapter.pair("olv-report", "olv-report")
        report = ConformanceSuite(adapter).run()
        payload = json.dumps({"adapter": report.adapter, "passed": report.passed,
                              "results": [{"name": item.name, "passed": item.passed, "detail": item.detail}
                                          for item in report.results]}, ensure_ascii=False)
        self.assertIn("capture_roundtrip_original", payload)


if __name__ == "__main__":
    unittest.main()
