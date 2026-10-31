"""Explicit source-backed temporal assertions. No model acceptance or persona mutation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3

from .candidates import CandidateDraft, CandidateWorkflow, EvidenceSpan
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json, _time


@dataclass(frozen=True)
class FactDecision:
    request_id: str
    subject_id: str
    predicate: str
    candidate_id: str
    confirmation_id: str
    expected_revision: int
    kind: str
    valid_from: str | None = None
    occurred_at: str | None = None
    valid_until: str | None = None
    replaces_id: str | None = None
    resolves: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    subject_id: str
    predicate: str
    revision: int
    kind: str
    anchor_id: str
    replaces_id: str | None
    candidate_id: str
    processor_version: str
    text: str = field(repr=False)
    valid_from: str | None
    valid_until: str | None
    occurred_at: str | None
    learned_at: str | None
    recorded_at: str
    sources: tuple[EvidenceSpan, ...]
    confirmation_id: str
    confirmation_version: int
    resolves: tuple[str, ...]


@dataclass(frozen=True)
class FactView:
    subject_id: str
    predicate: str
    revision: int
    knowledge_revision: int
    state: str
    fact: FactRecord | None
    plans: tuple[FactRecord, ...]
    disputes: tuple[FactRecord, ...]
    unavailable_ids: tuple[str, ...]
    clarification_required: bool
    valid_at: str
    known_at: str
    effective_until: str | None


class FactRegistry:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.workflow = CandidateWorkflow(runtime)

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS fact_heads_v1 (subject TEXT NOT NULL, predicate TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(subject,predicate))")
        db.execute("""CREATE TABLE IF NOT EXISTS fact_decisions_v1 (id TEXT PRIMARY KEY, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint BLOB NOT NULL, request TEXT NOT NULL,
            record TEXT NOT NULL, UNIQUE(subject,predicate,revision))""")

    @staticmethod
    def _record(raw: str) -> FactRecord:
        value = json.loads(raw)
        value["sources"] = tuple(EvidenceSpan(**span) for span in value["sources"])
        value["resolves"] = tuple(value["resolves"])
        return FactRecord(**value)

    @staticmethod
    def _rows(db: sqlite3.Connection, subject: str, predicate: str) -> tuple[FactRecord, ...]:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_decisions_v1'").fetchone():
            return ()
        rows = db.execute("SELECT record FROM fact_decisions_v1 WHERE subject=? AND predicate=? ORDER BY revision LIMIT 10001", (subject, predicate)).fetchall()
        if len(rows) > 10000:
            raise MemoryFailure("fact_scope_too_large")
        return tuple(FactRegistry._record(row["record"]) for row in rows)

    def _candidate(self, db: sqlite3.Connection, candidate_id: str) -> tuple[CandidateDraft, str]:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_candidates_v1'").fetchone():
            raise MemoryFailure("candidate_not_found")
        row = db.execute("SELECT run_id,draft FROM fact_candidates_v1 WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise MemoryFailure("candidate_not_found")
        run = self.workflow._row(db, row["run_id"])
        if run["state"] != "succeeded" or not self.workflow._current(db, run):
            raise MemoryFailure("stale_source")
        value = json.loads(row["draft"])
        value["sources"] = tuple(EvidenceSpan(**span) for span in value["sources"])
        draft = CandidateDraft(**value)
        self.workflow._validate(draft, self.workflow._sources(db, tuple(span.message_id for span in draft.sources)))
        if draft.provenance != "user_statement":
            raise MemoryFailure("fact_requires_user_statement")
        return draft, str(run["processor"])

    def _confirmation(self, db: sqlite3.Connection, message_id: str) -> tuple[int, str | None]:
        state = self.runtime._state(db, message_id)
        row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
        message = self.runtime._stored(row)
        if (state.status != "active" or message.event.role != "user" or message.event.completion != "complete"
                or not message.receipt.original_saved or not message.event.text.strip()):
            raise MemoryFailure("insufficient_evidence")
        return state.version, message.event.occurred_at

    @staticmethod
    def _validate(request: FactDecision) -> None:
        if not isinstance(request, FactDecision):
            raise MemoryFailure("invalid_fact_decision")
        for value in (request.request_id, request.subject_id, request.predicate, request.candidate_id, request.confirmation_id):
            _identifier(value)
        if request.replaces_id is not None:
            _identifier(request.replaces_id)
        if (type(request.expected_revision) is not int or request.expected_revision < 0
                or not isinstance(request.kind, str) or request.kind not in {"assert", "correction", "change", "plan", "disputed"}
                or not isinstance(request.resolves, tuple) or len(request.resolves) > 32
                or any(not isinstance(item, str) for item in request.resolves) or len(set(request.resolves)) != len(request.resolves)):
            raise MemoryFailure("invalid_fact_decision")
        for item in request.resolves:
            _identifier(item)
        for timestamp in (request.valid_from, request.valid_until, request.occurred_at):
            if timestamp is not None:
                _time(timestamp)
        if request.valid_until is not None and (request.valid_from is None or _time(request.valid_until) <= _time(request.valid_from)):
            raise MemoryFailure("invalid_fact_interval")
        if request.kind in {"change", "plan"} and request.valid_from is None:
            raise MemoryFailure("fact_time_required")
        if request.kind in {"assert", "plan", "disputed"} and request.replaces_id is not None:
            raise MemoryFailure("invalid_fact_predecessor")
        if request.kind in {"correction", "change"} and request.replaces_id is None:
            raise MemoryFailure("fact_predecessor_required")
        if request.kind in {"plan", "disputed"} and request.resolves:
            raise MemoryFailure("invalid_fact_resolution")

    def decide(self, access: Access, request: FactDecision) -> FactRecord:
        """Trusted maintenance boundary: the caller must establish the user's actual confirmation."""
        self.runtime._require_scope(access, "accept")
        self._validate(request)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(asdict(request)).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,record FROM fact_decisions_v1 WHERE id=?", (request.request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("fact_request_conflict")
                return self._record(existing["record"])
            rows = self._rows(db, request.subject_id, request.predicate)
            revision = rows[-1].revision if rows else 0
            if request.expected_revision != revision:
                raise MemoryFailure("fact_revision_conflict")
            draft, processor = self._candidate(db, request.candidate_id)
            if draft.disputed and request.kind != "disputed":
                raise MemoryFailure("candidate_disputed")
            confirmation_version, _ = self._confirmation(db, request.confirmation_id)
            source_times = [item.message.event.occurred_at for item in self.workflow._sources(db, tuple(span.message_id for span in draft.sources))]
            learned = max((item for item in source_times if item is not None), key=_time) if all(item is not None for item in source_times) else None
            actual = [row for row in rows if row.kind in {"assert", "change"}]
            by_id = {row.fact_id: row for row in rows}
            previous = by_id.get(request.replaces_id or "")
            anchor = request.request_id
            if request.kind == "assert" and actual:
                raise MemoryFailure("fact_conflict_requires_resolution")
            if request.kind in {"correction", "change"}:
                if previous is None or previous.kind not in {"assert", "change", "correction"}:
                    raise MemoryFailure("invalid_fact_predecessor")
                replacements = [row for row in rows if row.anchor_id == previous.anchor_id]
                if replacements[-1].fact_id != previous.fact_id:
                    raise MemoryFailure("fact_predecessor_superseded")
                if request.kind == "correction":
                    if (request.valid_from != previous.valid_from or request.valid_until != previous.valid_until
                            or request.occurred_at != previous.occurred_at):
                        raise MemoryFailure("correction_must_preserve_time")
                    anchor = previous.anchor_id
                else:
                    if not actual or previous.anchor_id != actual[-1].anchor_id:
                        raise MemoryFailure("fact_change_not_latest")
                    assert request.valid_from is not None
                    if previous.valid_from is not None and _time(request.valid_from) <= _time(previous.valid_from):
                        raise MemoryFailure("non_monotonic_fact_change")
            resolved = {item for row in rows for item in row.resolves}
            for item in request.resolves:
                if item not in by_id or by_id[item].kind not in {"plan", "disputed"} or item in resolved:
                    raise MemoryFailure("invalid_fact_resolution")
            now = datetime.now(timezone.utc).isoformat()
            if rows:
                # The knowledge watermark must never step backwards: a wall-clock regression between
                # decisions would fabricate knowledge views that never existed for this subject/predicate.
                now = max([now] + [row.recorded_at for row in rows], key=_time)
            record = FactRecord(request.request_id, request.subject_id, request.predicate, revision + 1, request.kind,
                anchor, request.replaces_id, request.candidate_id, processor, draft.text, request.valid_from, request.valid_until, request.occurred_at,
                learned, now, draft.sources, request.confirmation_id, confirmation_version, request.resolves)
            db.execute("INSERT INTO fact_decisions_v1 VALUES (?,?,?,?,?,?,?)", (record.fact_id, request.subject_id, request.predicate,
                record.revision, fingerprint, _json(asdict(request)), _json(asdict(record))))
            db.execute("INSERT INTO fact_heads_v1 VALUES (?,?,?) ON CONFLICT(subject,predicate) DO UPDATE SET revision=excluded.revision",
                (request.subject_id, request.predicate, record.revision))
            db.execute("COMMIT")
        return record

    def _available(self, db: sqlite3.Connection, record: FactRecord) -> bool:
        try:
            sources = self.workflow._sources(db, tuple(span.message_id for span in record.sources))
            self.workflow._validate(CandidateDraft(record.text, "user_statement", record.sources), sources)
            confirmation, _ = self._confirmation(db, record.confirmation_id)
            return confirmation == record.confirmation_version
        except MemoryFailure as error:
            if error.code in {"stale_source", "message_not_found", "candidate_span_mismatch", "protected_candidate",
                              "insufficient_evidence", "unsupported_extraction_source"}:
                return False
            raise

    def query(self, access: Access, subject_id: str, predicate: str, *, valid_at: str | None = None,
              known_at: str | None = None) -> FactView:
        self.runtime._require_scope(access, "recall")
        _identifier(subject_id)
        _identifier(predicate)
        now = datetime.now(timezone.utc).isoformat()
        at, known = now if valid_at is None else valid_at, now if known_at is None else known_at
        at_time, known_time = _time(at), _time(known)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            all_rows = self._rows(db, subject_id, predicate)
            rows = [row for row in all_rows if _time(row.recorded_at) <= known_time]
            resolved = {item for row in rows for item in row.resolves}
            anchors: dict[str, FactRecord] = {}
            for row in rows:
                if row.kind in {"assert", "change", "correction"}:
                    anchors[row.anchor_id] = row
            # Do not filter unavailable successors before choosing time: that would resurrect old facts.
            roots = [row for row in rows if row.kind in {"assert", "change"}]
            eligible = [row for row in roots if row.valid_from is not None and _time(row.valid_from) <= at_time]
            chosen = anchors[eligible[-1].anchor_id] if eligible else None
            unknown = [row for row in roots if row.valid_from is None]
            if chosen is None and unknown and valid_at is None:
                chosen = anchors[unknown[-1].anchor_id]
            if chosen is not None and chosen.valid_until is not None and _time(chosen.valid_until) <= at_time:
                chosen = None
            effective_until = chosen.valid_until if chosen is not None else None
            if chosen is not None:
                successors = [row.valid_from for row in roots if row.valid_from is not None
                    and row.anchor_id != chosen.anchor_id and (chosen.valid_from is None or _time(row.valid_from) > _time(chosen.valid_from))]
                boundaries = successors + ([effective_until] if effective_until is not None else [])
                effective_until = min(boundaries, key=_time) if boundaries else None
            unavailable: list[str] = []
            available: dict[str, bool] = {}
            def visible(row: FactRecord) -> bool:
                if row.fact_id not in available:
                    available[row.fact_id] = self._available(db, row)
                    if not available[row.fact_id]:
                        unavailable.append(row.fact_id)
                return available[row.fact_id]
            plans = tuple(row for row in rows if row.kind == "plan" and row.fact_id not in resolved and visible(row))
            disputes = tuple(row for row in rows if row.kind == "disputed" and row.fact_id not in resolved
                and (row.valid_from is None or _time(row.valid_from) <= at_time)
                and (row.valid_until is None or _time(row.valid_until) > at_time) and visible(row))
            state = "current" if valid_at is None else "historical"
            if chosen is None:
                state = "unknown_time" if unknown else "no_fact"
            elif not visible(chosen):
                chosen, state = None, "source_unavailable"
            elif chosen.valid_from is None:
                state = "unknown_time"
            if disputes:
                state = "disputed"
            return FactView(subject_id, predicate, all_rows[-1].revision if all_rows else 0,
                rows[-1].revision if rows else 0, state, chosen, plans, disputes, tuple(unavailable), bool(disputes), at, known,
                effective_until if chosen is not None else None)
