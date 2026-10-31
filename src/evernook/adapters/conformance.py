"""Conformance suite: fixed checks any shell adapter must pass per adapter-conformance-v1."""
from __future__ import annotations

from dataclasses import dataclass

from ..context import ContextPolicy
from ..models import Event, MemoryFailure, Source
from .surface import AdapterSurface


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ConformanceReport:
    adapter: str
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


class _Counter:
    counter_id = "conformance-codepoint-units-v1"

    def count(self, text: str) -> int:
        return len(text)


class ConformanceSuite:
    def __init__(self, adapter: AdapterSurface):
        self.adapter = adapter
        self._sequence_number = 0

    def run(self) -> ConformanceReport:
        checks = (
            self._capture_roundtrip_original(),
            self._capture_idempotent_retry(),
            self._capture_sensitive_projection(),
            self._injection_holds_budget_and_reports_omissions(),
            self._injection_excludes_suppressed_profile_tags(),
            self._scope_discipline(),
            self._gap_disclosure_declares_uncovered(),
        )
        return ConformanceReport(self.adapter.name, checks)

    def _text_event(self, text: str, *, role: str = "user") -> Event:
        self._sequence_number += 1
        source = Source("conformance", "suite", "main", str(self._sequence_number))
        return Event(source, role, self._sequence_number, "2026-10-30T12:00:00+08:00", "complete", text)

    def _capture_roundtrip_original(self) -> CheckResult:
        text = "合成：契约原文 37.50 元🙂"
        receipt = self.adapter.capture(self._text_event(text))
        stored = self.adapter.read_back(receipt.message.message_id)
        ok = stored is not None and stored.event.text == text and stored.receipt.original_saved
        return CheckResult("capture_roundtrip_original", ok,
                           "read-back equals the submitted original" if ok else "original text not preserved")

    def _capture_idempotent_retry(self) -> CheckResult:
        event = self._text_event("合成：幂等重试样本")
        first = self.adapter.capture(event)
        second = self.adapter.capture(event)
        ok = first.message.message_id == second.message.message_id
        return CheckResult("capture_idempotent_retry", ok, "retry returns the original receipt" if ok
                           else "retry produced a new message id")

    def _capture_sensitive_projection(self) -> CheckResult:
        receipt = self.adapter.capture(self._text_event("password=synthetic-conformance-1"))
        ok = not receipt.message.original_saved and receipt.message.status == "projection_only"
        return CheckResult("capture_sensitive_projection", ok, "sensitive input saved as projection" if ok
                           else "sensitive input must not claim original_saved")

    def _injection_holds_budget_and_reports_omissions(self) -> CheckResult:
        policy = ContextPolicy(512, 512, 64, 7, 512, persona_tokens=16)
        bundle = self.adapter.inject("合成", policy, _Counter())
        within = "untrusted_memory_context" in bundle.payload and bundle.used_tokens <= bundle.token_limit
        honest_over_budget = bundle.payload == "" and "context_envelope_over_budget" in bundle.degraded
        ok = within or honest_over_budget
        return CheckResult("injection_holds_budget_and_reports_omissions", ok,
                           f"used={bundle.used_tokens} limit={bundle.token_limit}" if within
                           else "payload cleared with explicit envelope_over_budget" if honest_over_budget
                           else "payload not an untrusted envelope or budget exceeded silently")

    def _injection_excludes_suppressed_profile_tags(self) -> CheckResult:
        import json
        bundle = self.adapter.inject("合成", ContextPolicy(512, 512, 64, 7, 512, persona_tokens=16,
                                                           profile_tokens=256), _Counter())
        if bundle.payload == "" and "context_envelope_over_budget" in bundle.degraded:
            return CheckResult("injection_excludes_suppressed_profile_tags", True,
                               "payload cleared explicitly; nothing injected at all")
        try:
            payload = json.loads(bundle.payload)
            items = payload.get("items", [])
            ok = all(item["evidence_id"] for item in items if item["kind"] == "profile")
        except ValueError:
            ok = False
        return CheckResult("injection_excludes_suppressed_profile_tags", ok,
                           "injected profile items carry resolved evidence ids" if ok
                           else "payload is not parseable or profile items lack evidence ids")

    def _scope_discipline(self) -> CheckResult:
        try:
            code = self.adapter.attempt_out_of_scope()
        except MemoryFailure as failure:
            code = failure.code
        ok = code == "permission_denied"
        return CheckResult("scope_discipline", ok, f"out-of-scope attempt returned {code}" if ok
                           else f"out-of-scope attempt must fail with permission_denied, got {code}")

    def _gap_disclosure_declares_uncovered(self) -> CheckResult:
        covered, gaps = set(self.adapter.covered_events()), self.adapter.gaps()
        structured = all(isinstance(gap.event_kind, str) and gap.event_kind and isinstance(gap.reason, str) and gap.reason
                         for gap in gaps)
        disjoint = not (covered & {gap.event_kind for gap in gaps})
        ok = bool(gaps) and structured and disjoint
        kinds = ",".join(sorted(gap.event_kind for gap in gaps))
        return CheckResult("gap_disclosure_declares_uncovered", ok, f"gaps=[{kinds}]" if ok
                           else "gaps must be non-empty, structured and disjoint from covered events")
