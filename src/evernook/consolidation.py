"""Explicit consolidation workflow: raw jobs to review-queue drafts. No scheduler, no auto-acceptance.

The auto tier marks whitelisted topic/navigation_summary drafts `merged`, but nothing consumes those
rows yet — they remain queue entries; navigation/experience integration is later work. Only the
profile_tag auto path has a live consumer (confirmed tags reach the must-read profile summary)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3

from .candidates import CandidateDraft, CandidateLease, CandidateRun, CandidateWorkflow, EvidenceSpan
from .models import Access, MemoryFailure
from .profile import ProfileDecision, ProfileDraft, ProfileRegistry
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal


CONSOLIDATION_KINDS = ("profile_tag", "topic", "navigation_summary")
CONSOLIDATION_TIERS = ("manual", "review", "auto")
# Low-risk merge universe: tiers may narrow the whitelist below but never widen it, so facts,
# experience, persona and non-interest profile categories can never skip user confirmation.
LOW_RISK_KINDS = ("topic", "navigation_summary")
LOW_RISK_PROFILE_CATEGORIES = ("interest",)


@dataclass(frozen=True)
class ConsolidationDraft:
    kind: str
    label: str = field(repr=False)
    sources: tuple[EvidenceSpan, ...]
    detail: str = field(default="", repr=False)
    category: str = "interest"
    confidence: float = 0.5
    disputed: bool = False


@dataclass(frozen=True)
class ConsolidationDraftRow:
    candidate_id: str
    run_id: str
    kind: str
    label: str = field(repr=False)
    detail: str = field(repr=False)
    category: str
    confidence: float
    state: str
    proposal_id: str | None
    created_at: str
    sources: tuple[EvidenceSpan, ...]


@dataclass(frozen=True)
class ConsolidationQueuePage:
    rows: tuple[ConsolidationDraftRow, ...]
    next_offset: int | None


@dataclass(frozen=True)
class ConsolidationResult:
    run: CandidateRun
    drafts: tuple[ConsolidationDraftRow, ...]


class ConsolidationWorkflow:
    """Lease and raw-source semantics ride the candidate contract; drafts land in the review queue only."""

    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.candidates = CandidateWorkflow(runtime)
        self.profile = ProfileRegistry(runtime)

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS consolidation_drafts_v1 (
            candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
            detail TEXT NOT NULL, category TEXT NOT NULL, confidence REAL NOT NULL, state TEXT NOT NULL,
            proposal_id TEXT, created_at TEXT NOT NULL, sources TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS consolidation_drafts_run_v1 ON consolidation_drafts_v1(run_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS consolidation_results_v1 (
            run_id TEXT PRIMARY KEY REFERENCES candidate_runs_v1(id), candidate_ids TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS consolidation_settings_v1 (
            id INTEGER PRIMARY KEY CHECK(id=1), tier TEXT NOT NULL, whitelist TEXT NOT NULL)""")

    @staticmethod
    def _settings(db: sqlite3.Connection) -> tuple[str, dict[str, set[str]]]:
        row = db.execute("SELECT tier,whitelist FROM consolidation_settings_v1 WHERE id=1").fetchone()
        if row is None:
            return "review", {"kinds": set(LOW_RISK_KINDS), "profile_categories": set(LOW_RISK_PROFILE_CATEGORIES)}
        whitelist = json.loads(row["whitelist"])
        return str(row["tier"]), {"kinds": set(whitelist["kinds"]), "profile_categories": set(whitelist["profile_categories"])}

    def configure(self, access: Access, *, tier: str, kinds: tuple[str, ...] | None = None,
                  profile_categories: tuple[str, ...] | None = None) -> dict[str, object]:
        """Trusted maintenance boundary; the whitelist may only narrow the low-risk universe."""
        self.runtime._require_scope(access, "maintain", "consolidation_configure")
        if not isinstance(tier, str) or tier not in CONSOLIDATION_TIERS:
            raise MemoryFailure("invalid_consolidation_tier")
        if kinds is None:
            kinds = LOW_RISK_KINDS
        if profile_categories is None:
            profile_categories = LOW_RISK_PROFILE_CATEGORIES
        if (not isinstance(kinds, tuple) or any(item not in LOW_RISK_KINDS for item in kinds)
                or not isinstance(profile_categories, tuple)
                or any(item not in LOW_RISK_PROFILE_CATEGORIES for item in profile_categories)):
            raise MemoryFailure("invalid_consolidation_whitelist")
        whitelist = _json({"kinds": sorted(set(kinds)), "profile_categories": sorted(set(profile_categories))})
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            db.execute("""INSERT INTO consolidation_settings_v1 VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET
                tier=excluded.tier, whitelist=excluded.whitelist""", (tier, whitelist))
            db.execute("COMMIT")
        return {"tier": tier, "kinds": sorted(set(kinds)), "profile_categories": sorted(set(profile_categories))}

    def begin(self, access: Access, source_ids: tuple[str, ...], *, processor_version: str = "consolidation-explicit-v1",
              lease_seconds: int = 300) -> CandidateLease:
        return self.candidates.begin(access, source_ids, processor_version=processor_version, lease_seconds=lease_seconds)

    def fail(self, access: Access, run_id: str, token: str, *, error_code: str = "extractor_failed") -> CandidateRun:
        return self.candidates.fail(access, run_id, token, error_code=error_code)

    def status(self, access: Access, run_id: str) -> CandidateRun:
        return self.candidates.status(access, run_id)

    @staticmethod
    def _validate(draft: ConsolidationDraft) -> None:
        if (not isinstance(draft, ConsolidationDraft) or not isinstance(draft.kind, str) or draft.kind not in CONSOLIDATION_KINDS
                or not isinstance(draft.label, str) or not 1 <= len(draft.label.strip()) <= 256 or len(draft.label) > 256
                or not isinstance(draft.detail, str) or len(draft.detail) > 4096
                or not isinstance(draft.category, str) or not 1 <= len(draft.category) <= 64
                or type(draft.confidence) is not float or not 0 <= draft.confidence <= 1
                or type(draft.disputed) is not bool or not isinstance(draft.sources, tuple) or not 1 <= len(draft.sources) <= 16):
            raise MemoryFailure("invalid_consolidation_draft")
        _identifier(draft.category)
        try:
            (draft.label + draft.detail).encode("utf-8", errors="strict")
        except UnicodeError:
            raise MemoryFailure("invalid_encoding") from None
        if credential_signal(draft.label) or credential_signal(draft.detail):
            raise MemoryFailure("protected_consolidation_draft")

    @staticmethod
    def _candidate_id(runtime: MemoryRuntime, run_id: str, draft: CandidateDraft, source: ConsolidationDraft) -> str:
        # The mapped draft alone drops kind/category/confidence: two distinct drafts could then
        # collide on one candidate row and one proposal request. The original draft disambiguates.
        payload = _json([run_id, asdict(draft), source.kind, source.category, source.confidence])
        return hmac.digest(runtime._fingerprint_key, payload.encode("utf-8"), hashlib.sha256).hex()

    @staticmethod
    def _draft_row(row: sqlite3.Row) -> ConsolidationDraftRow:
        return ConsolidationDraftRow(row["candidate_id"], row["run_id"], row["kind"], row["label"],
            row["detail"], row["category"], float(row["confidence"]), row["state"], row["proposal_id"],
            row["created_at"], tuple(EvidenceSpan(**span) for span in json.loads(row["sources"])))

    def _result(self, db: sqlite3.Connection, run: CandidateRun) -> ConsolidationResult:
        if run.state != "succeeded":
            raise MemoryFailure("incomplete_consolidation")
        marker = db.execute("SELECT candidate_ids FROM consolidation_results_v1 WHERE run_id=?", (run.run_id,)).fetchone()
        if marker is None:
            if run.candidate_count == 0:
                return ConsolidationResult(run, ())
            raise MemoryFailure("incomplete_consolidation")
        rows = {row["candidate_id"]: row for row in db.execute(
            "SELECT * FROM consolidation_drafts_v1 WHERE run_id=?", (run.run_id,))}
        expected = json.loads(marker["candidate_ids"])
        if set(expected) != set(rows):
            raise MemoryFailure("incomplete_consolidation")
        return ConsolidationResult(run, tuple(self._draft_row(rows[item]) for item in expected))

    def result(self, access: Access, run_id: str) -> ConsolidationResult:
        """Read proof that all assets, rather than only the candidate run, completed."""
        self.runtime._require_scope(access, "maintain", "consolidation_result")
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            run = self.candidates._status(db, self.candidates._row(db, run_id))
            result = self._result(db, run)
            db.execute("COMMIT")
            return result

    def complete(self, access: Access, run_id: str, token: str, drafts: tuple[ConsolidationDraft, ...]) -> ConsolidationResult:
        """Commit candidate success, profile effects and the full review queue atomically."""
        if not isinstance(token, str):
            raise MemoryFailure("invalid_candidate_batch")
        return self._complete(access, run_id, token, drafts)

    def recover(self, access: Access, run_id: str,
                provider: Callable[[CandidateLease], tuple[ConsolidationDraft, ...]]) -> ConsolidationResult:
        """Repair legacy partial runs only when regenerated drafts match their committed candidates."""
        self.runtime._require_scope(access, "accept", "consolidation_recover")
        identity = self.runtime._require_scope(access, "maintain", "consolidation_recover")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            row = self.candidates._row(db, run_id)
            run = self.candidates._status(db, row)
            if row["owner"] != identity.client_id or run.state != "succeeded":
                raise MemoryFailure("invalid_candidate_lease")
            sources = self.candidates._sources(db, run.source_ids)
        drafts = provider(CandidateLease(run, None, sources))
        return self._complete(access, run_id, None, drafts)

    def _complete(self, access: Access, run_id: str, token: str | None,
                  drafts: tuple[ConsolidationDraft, ...]) -> ConsolidationResult:
        self.runtime._require_scope(access, "accept", "consolidation_complete")
        identity = self.runtime._require_scope(access, "maintain", "candidate_complete")
        if not isinstance(drafts, tuple) or not 1 <= len(drafts) <= 32:
            raise MemoryFailure("invalid_consolidation_batch")
        for draft in drafts:
            self._validate(draft)
        mapped = tuple(CandidateDraft(
            (draft.label + "\n" + draft.detail).strip() if draft.detail else draft.label,
            "user_statement" if draft.kind == "profile_tag" else "quoted",
            draft.sources, draft.disputed) for draft in drafts)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            lease_row = self.candidates._row(db, run_id)
            run = self.candidates._complete(db, identity.client_id, run_id, token, mapped)
            self._schema(db)
            if db.execute("SELECT 1 FROM consolidation_results_v1 WHERE run_id=?", (run_id,)).fetchone():
                return self._result(db, run)
            tier, whitelist = self._settings(db)
            now = datetime.now(timezone.utc).isoformat()
            candidate_ids: list[str] = []
            for draft, mapped_draft in zip(drafts, mapped):
                candidate_id = self._candidate_id(self.runtime, run_id, mapped_draft, draft)
                if candidate_id in candidate_ids:
                    continue
                candidate_ids.append(candidate_id)
                if db.execute("SELECT 1 FROM consolidation_drafts_v1 WHERE candidate_id=?", (candidate_id,)).fetchone():
                    continue
                proposal_id: str | None = None
                state = "disputed" if draft.disputed else "pending_review"
                if tier != "manual" and draft.kind == "profile_tag" and not draft.disputed:
                    proposal = self.profile._decide(db, ProfileDecision(
                        f"consolidation-{candidate_id}", f"consol-{candidate_id[:32]}", 0, "propose",
                        ProfileDraft(draft.category, draft.label, draft.confidence, draft.sources)))
                    proposal_id = proposal.tag_id
                    if tier == "auto" and draft.category in whitelist["profile_categories"]:
                        self.profile._decide(db, ProfileDecision(
                            f"consolidation-auto-{candidate_id}", proposal.tag_id, 1, "auto_confirm"))
                        state = "merged"
                elif tier == "auto" and draft.kind in whitelist["kinds"] and not draft.disputed:
                    state = "merged"
                db.execute("INSERT INTO consolidation_drafts_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate_id, run_id, draft.kind, draft.label, draft.detail, draft.category,
                     draft.confidence, state, proposal_id, now, _json([asdict(span) for span in draft.sources])))
            db.execute("INSERT INTO consolidation_results_v1 VALUES (?,?)", (run_id, _json(candidate_ids)))
            result = self._result(db, run)
            if token is not None:
                self.candidates._lease(lease_row, identity.client_id, token)
            db.execute("COMMIT")
            return result

    def queue(self, access: Access, *, limit: int = 100, offset: int = 0) -> ConsolidationQueuePage:
        """Review queue read; drafts never enter recall."""
        self.runtime._require_scope(access, "maintain", "consolidation_queue")
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise MemoryFailure("invalid_page")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='consolidation_drafts_v1'").fetchone():
                return ConsolidationQueuePage((), None)
            rows = db.execute("""SELECT candidate_id,run_id,kind,label,detail,category,confidence,state,
                proposal_id,created_at,sources FROM consolidation_drafts_v1 ORDER BY created_at,candidate_id
                LIMIT ? OFFSET ?""", (limit + 1, offset)).fetchall()
            result: list[ConsolidationDraftRow] = []
            for row in rows[:limit]:
                result.append(self._draft_row(row))
            return ConsolidationQueuePage(tuple(result), offset + limit if len(rows) > limit else None)
