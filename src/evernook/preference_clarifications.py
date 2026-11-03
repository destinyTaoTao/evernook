"""Trusted explicit preference clarification using existing fact/profile event streams."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac

from .candidates import PREFERENCE_CATEGORIES
from .facts import FactDecision, FactRecord, FactRegistry
from .models import Access, MemoryFailure
from .profile import PREFERENCE_CLARIFICATION_PROCESSOR, ProfileDecision, ProfileDraft, ProfileRecord, ProfileRegistry
from .runtime import MemoryRuntime, _identifier, _json, _time


@dataclass(frozen=True)
class OpenClarification:
    subject_id: str
    predicate: str
    profile_tag_id: str
    candidate_id: str
    confirmation_id: str
    expected_fact_revision: int
    expected_profile_revision: int


@dataclass(frozen=True)
class ResolveClarification:
    subject_id: str
    predicate: str
    profile_tag_id: str
    clarification_id: str
    confirmation_id: str
    expected_fact_revision: int
    expected_profile_revision: int
    kind: str
    choice: str
    valid_from: str | None = None


@dataclass(frozen=True)
class PreferenceClarification:
    clarification_id: str
    status: str
    fact: FactRecord
    profile: ProfileRecord


class PreferenceClarifications:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.facts, self.profiles = FactRegistry(runtime), ProfileRegistry(runtime)

    def _id(self, kind: str, *parts: str) -> str:
        return "preference-" + kind + ":" + hmac.digest(
            self.runtime._fingerprint_key, _json(parts).encode(), hashlib.sha256).hex()

    def open(self, access: Access, request: OpenClarification) -> PreferenceClarification:
        """The trusted caller establishes that these two statements concern one conflicting preference."""
        self.runtime._require_scope(access, "accept", "preference_clarification_open")
        if not isinstance(request, OpenClarification):
            raise MemoryFailure("invalid_preference_clarification")
        for value in (request.subject_id, request.predicate, request.profile_tag_id, request.candidate_id, request.confirmation_id):
            _identifier(value)
        if any(type(value) is not int or value < 0 for value in (request.expected_fact_revision, request.expected_profile_revision)):
            raise MemoryFailure("invalid_preference_clarification")
        clarification_id = self._id("dispute", request.subject_id, request.predicate, request.profile_tag_id, request.candidate_id)
        processor = PREFERENCE_CLARIFICATION_PROCESSOR + clarification_id
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            incoming, _ = self.facts._candidate(db, request.candidate_id)
            if incoming.preference_category not in PREFERENCE_CATEGORIES or incoming.disputed:
                raise MemoryFailure("preference_candidate_required")
            view = self.facts._query_snapshot(db, request.subject_id, request.predicate)
            profile = self.profiles._view(db, request.profile_tag_id)
            if any(clarification_id in row.resolves for row in self.facts._rows(db, request.subject_id, request.predicate)):
                raise MemoryFailure("preference_clarification_resolved")
            existing = next((row for row in view.disputes if row.fact_id == clarification_id), None)
            if existing is not None:
                if (existing.confirmation_id != request.confirmation_id or existing.revision != request.expected_fact_revision + 1
                        or profile.record is None or profile.state != "draft" or profile.revision != request.expected_profile_revision + 1
                        or profile.record.draft.processor_version != processor or view.fact is None):
                    raise MemoryFailure("preference_clarification_conflict")
                return PreferenceClarification(clarification_id, "pending", existing, profile.record)
            if view.clarification_required:
                raise MemoryFailure("preference_clarification_pending")
            if (view.fact is None or profile.state != "confirmed" or profile.record is None
                    or profile.record.draft.sources != view.fact.sources):
                raise MemoryFailure("preference_source_mismatch")
            expected_category = "interest" if incoming.preference_category == "general_interest" else "preference"
            if profile.record.draft.category != expected_category or incoming.text == view.fact.text:
                raise MemoryFailure("preference_conflict_required")
            disputed = self.facts._decide(db, FactDecision(clarification_id, request.subject_id, request.predicate,
                request.candidate_id, request.confirmation_id, request.expected_fact_revision, "disputed"))
            proposed = self.profiles._decide(db, ProfileDecision(self._id("draft", clarification_id), request.profile_tag_id,
                request.expected_profile_revision, "revise", ProfileDraft(expected_category, incoming.text, 1.0, incoming.sources, processor)))
            db.execute("COMMIT")
        return PreferenceClarification(clarification_id, "pending", disputed, proposed)

    def resolve(self, access: Access, request: ResolveClarification) -> PreferenceClarification:
        """Confirm the reviewed incoming draft as correction or real change, atomically."""
        self.runtime._require_scope(access, "accept", "preference_clarification_resolve")
        if (not isinstance(request, ResolveClarification) or not isinstance(request.kind, str)
                or request.kind not in {"correction", "change"} or not isinstance(request.choice, str)
                or request.choice not in {"incoming", "existing"}):
            raise MemoryFailure("invalid_preference_resolution")
        for value in (request.subject_id, request.predicate, request.profile_tag_id, request.clarification_id, request.confirmation_id):
            _identifier(value)
        if (any(type(value) is not int or value < 0 for value in (request.expected_fact_revision, request.expected_profile_revision))
                or (request.kind == "correction" and request.valid_from is not None)
                or (request.choice == "existing" and request.kind != "correction")):
            raise MemoryFailure("invalid_preference_resolution")
        if request.kind == "change" and request.valid_from is not None and _time(request.valid_from) > datetime.now(timezone.utc).timestamp():
            raise MemoryFailure("preference_future_change_requires_plan")
        resolution_id = self._id("resolve", request.clarification_id, request.profile_tag_id)
        profile_id = self._id("confirm", request.clarification_id)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = self.facts._rows(db, request.subject_id, request.predicate)
            dispute = next((row for row in rows if row.fact_id == request.clarification_id and row.kind == "disputed"), None)
            if dispute is None:
                raise MemoryFailure("preference_clarification_not_found")
            if not dispute.sources:
                raise MemoryFailure("preference_source_unavailable")
            if self._id("dispute", request.subject_id, request.predicate, request.profile_tag_id,
                        dispute.candidate_id) != request.clarification_id:
                raise MemoryFailure("preference_clarification_not_found")
            incoming, _ = self.facts._candidate(db, dispute.candidate_id)
            profile = self.profiles._view(db, request.profile_tag_id)
            existing = next((row for row in rows if row.fact_id == resolution_id), None)
            if existing is not None:
                if (not self.facts._available(db, existing) or profile.record is None or profile.state != "confirmed"
                        or profile.record.request_id != profile_id):
                    raise MemoryFailure("preference_clarification_changed")
                previous = next((row for row in rows if row.fact_id == existing.replaces_id), None)
            else:
                view = self.facts._query_snapshot(db, request.subject_id, request.predicate)
                if (dispute not in view.disputes or profile.state != "draft" or profile.record is None
                        or profile.record.draft.processor_version != PREFERENCE_CLARIFICATION_PROCESSOR + request.clarification_id):
                    raise MemoryFailure("preference_clarification_changed")
                previous = view.fact
            if previous is None:
                raise MemoryFailure("preference_source_unavailable")
            selected, _ = self.facts._candidate(db, previous.candidate_id) if request.choice == "existing" else (incoming, "")
            candidate_id = previous.candidate_id if request.choice == "existing" else dispute.candidate_id
            correction = request.kind == "correction"
            resolved = self.facts._decide(db, FactDecision(resolution_id, request.subject_id, request.predicate,
                candidate_id, request.confirmation_id, request.expected_fact_revision, request.kind,
                valid_from=previous.valid_from if correction else request.valid_from,
                valid_until=previous.valid_until if correction else None, occurred_at=previous.occurred_at if correction else None,
                replaces_id=previous.fact_id, resolves=(request.clarification_id,), category=previous.category))
            confirmed = self.profiles._decide(db, ProfileDecision(profile_id, request.profile_tag_id,
                request.expected_profile_revision, "clarify", ProfileDraft(profile.record.draft.category,
                    selected.text, 1.0, selected.sources, PREFERENCE_CLARIFICATION_PROCESSOR + request.clarification_id),
                confirmation_id=request.confirmation_id))
            db.execute("COMMIT")
        return PreferenceClarification(request.clarification_id, "resolved", resolved, confirmed)
