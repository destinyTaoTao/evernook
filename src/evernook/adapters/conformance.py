"""Conformance suite: fixed checks any shell adapter must pass per adapter-conformance-v1."""
from __future__ import annotations

from dataclasses import dataclass
import json
from uuid import uuid4

from ..context import ContextBundle, ContextPolicy, TokenCounter
from ..models import Access, Event, MemoryFailure, Source
from ..candidates import EvidenceSpan
from ..profile import ProfileDecision, ProfileDraft, ProfileRegistry
from ..runtime import MemoryRuntime
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
    counter_id: str = ""
    counter_scope: str = "synthetic_contract_only"

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


class _Counter:
    counter_id = "conformance-codepoint-units-v1"

    def count(self, text: str) -> int:
        return len(text)


class ConformanceSuite:
    def __init__(self, adapter: AdapterSurface, *, profile_runtime: MemoryRuntime | None = None,
                 profile_access: Access | None = None, counter: TokenCounter | None = None):
        self.adapter = adapter
        self._sequence_number = 0
        self._session_id = "suite-" + uuid4().hex
        self.profile_runtime, self.profile_access = profile_runtime, profile_access
        self.counter = counter if counter is not None else _Counter()
        self.counter_scope = "synthetic_contract_only" if counter is None else "supplied_counter_not_model_verified"

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
        return ConformanceReport(self.adapter.name, checks, self.counter.counter_id, self.counter_scope)

    def _text_event(self, text: str, *, role: str = "user") -> Event:
        self._sequence_number += 1
        source = Source("conformance", self._session_id, "main", str(self._sequence_number))
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
        bundle = self.adapter.inject("合成", policy, self.counter)
        ok = self._honest_bundle(bundle, policy)
        return CheckResult("injection_holds_budget_and_reports_omissions", ok,
                           f"independent_count={self.counter.count(bundle.payload)} requested_limit={policy.normal_tokens}"
                           if ok else "payload envelope, independent count or requested budget mismatch")

    def _honest_bundle(self, bundle: ContextBundle, policy: ContextPolicy) -> bool:
        actual = self.counter.count(bundle.payload)
        if (type(actual) is not int or actual < 0 or (bundle.payload and actual == 0)
                or type(bundle.used_tokens) is not int or type(bundle.token_limit) is not int
                or bundle.used_tokens != actual or bundle.token_limit != policy.normal_tokens
                or actual > policy.normal_tokens or bundle.mode != "normal" or bundle.counter_id != self.counter.counter_id):
            return False
        if not bundle.payload:
            return "context_envelope_over_budget" in bundle.degraded and not bundle.complete and bundle.evidence_count == 0
        def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        try:
            payload = json.loads(bundle.payload, object_pairs_hook=unique_keys)
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("kind") == "untrusted_memory_context" and isinstance(payload.get("items"), list)

    def _injection_excludes_suppressed_profile_tags(self) -> CheckResult:
        if self.profile_runtime is None or self.profile_access is None:
            return CheckResult("injection_excludes_suppressed_profile_tags", False,
                               "independent trusted profile fixture required; adapter identity remains unprivileged")
        tag_id = "conformance-profile-" + uuid4().hex
        marker = "synthetic-profile-marker-" + uuid4().hex
        source = self.adapter.capture(self._text_event(marker)).message
        registry = ProfileRegistry(self.profile_runtime)
        registry.decide(self.profile_access, ProfileDecision("propose-" + tag_id, tag_id, 0, "propose",
            ProfileDraft("interest", marker, 0.9, (EvidenceSpan(source.message_id, 1, "original", 1, 0, len(marker), marker),))))
        confirmation = self.adapter.capture(self._text_event("合成：确认符合性测试画像")).message
        registry.decide(self.profile_access, ProfileDecision("confirm-" + tag_id, tag_id, 1, "confirm",
            confirmation_id=confirmation.message_id))
        policy = ContextPolicy(4096, 4096, 64, 7, 512, profile_tokens=2048)
        before = self.adapter.inject("", policy, self.counter)
        before_ok = self._honest_bundle(before, policy) and bool(before.payload)
        try:
            items = json.loads(before.payload)["items"]
            before_ok = before_ok and any(item.get("kind") == "profile" and item.get("evidence_id") == "profile:" + tag_id
                and item.get("data", {}).get("label") == marker for item in items)
        except (ValueError, KeyError, TypeError, AttributeError):
            before_ok = False
        suppression = self.adapter.capture(self._text_event("合成：抑制符合性测试画像")).message
        registry.decide(self.profile_access, ProfileDecision("suppress-" + tag_id, tag_id, 2, "suppress",
            confirmation_id=suppression.message_id))
        after = self.adapter.inject("", policy, self.counter)
        after_ok = self._honest_bundle(after, policy)
        decoded = json.dumps(json.loads(after.payload), ensure_ascii=False) if after_ok and after.payload else ""
        ok = before_ok and after_ok and tag_id not in decoded and marker not in decoded
        return CheckResult("injection_excludes_suppressed_profile_tags", ok,
                           "confirmed fixture visible before suppression; identity and unique content absent afterward" if ok
                           else "profile fixture missing before suppression or suppressed identity/content still injected")

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
