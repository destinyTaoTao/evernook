"""Finite offline preference rules and gated trusted publication."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3
import re
import threading
import time

from .candidates import CandidateDraft, CandidateRun, CandidateWorkflow, ExtractionSource, PREFERENCE_CATEGORIES
from .consolidation import ConsolidationWorkflow
from .extraction import _remaining
from .models import Access, MemoryFailure
from .preferences import PreferenceProcessor, _category
from .facts import FactDecision, FactRegistry, FactView
from .profile import PREFERENCE_CLARIFICATION_PROCESSOR, ProfileDecision, ProfileDraft, ProfileRegistry
from .preference_clarifications import OpenClarification, PreferenceClarifications
from .runtime import MemoryRuntime, _identifier, _json, _time


AUTOMATIC_PREFERENCE_PROCESSOR_PREFIX = 'automatic-preference-rules-v1:'
_LEGACY_REVIEW_PREFIX = 'preference-review-v1:'


def _statement(text: str) -> tuple[str | None, bool, str]:
    positive = re.sub(r'^我(?:不再|不)(偏好用|偏好|喜欢|爱好|常用|习惯用)', r'我\1', text.strip())
    return _category(positive), positive != text.strip(), positive


class PreferenceRules:
    """Whole statements only; classification stays in the shared finite preference policy."""
    def __init__(self) -> None:
        self.processor_version = 'preference-rules-v1:' + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def select(self, sources: tuple[ExtractionSource, ...], *, timeout: float,
               cancel: threading.Event | None) -> object:
        return {'selections': [{'evidence': [{'source': index, 'quote': item.message.event.text}],
            'provenance': 'user_statement', 'disputed': False} for index, item in enumerate(sources)
            if item.message.event.role == 'user']}


class _RuleProcessor(PreferenceProcessor):
    @staticmethod
    def _classify(text: str) -> str | None:
        return _statement(text)[0]

    @property
    def processor_version(self) -> str:
        return AUTOMATIC_PREFERENCE_PROCESSOR_PREFIX + hashlib.sha256(super().processor_version.encode()).hexdigest()


@dataclass(frozen=True)
class PreferenceUpdate:
    candidate_id: str
    state: str
    reason: str | None
    profile_tag_id: str | None = None
    clarification_id: str | None = None
    predicate: str | None = None


@dataclass(frozen=True)
class PreferenceUpdateReport:
    runs: tuple[CandidateRun, ...]
    updates: tuple[PreferenceUpdate, ...]
    error_code: str | None


@dataclass(frozen=True)
class PreferenceUpdateSettings:
    enabled: bool
    categories: tuple[str, ...]
    admitted_processor: str | None
    rules_processor: str


class PreferenceUpdates:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.processor = _RuleProcessor(runtime, PreferenceRules())

    @staticmethod
    def classify_statement(text: str) -> str | None:
        return _statement(text)[0]

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS preference_updates_settings_v1 (
            id INTEGER PRIMARY KEY CHECK(id=1),categories TEXT NOT NULL,admitted_processor TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS preference_update_receipts_v1 (
            candidate_id TEXT PRIMARY KEY,processor TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL,checked_at REAL NOT NULL,
            slot TEXT,slot_value TEXT)''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(preference_update_receipts_v1)')}
        for column in ('slot', 'slot_value'):
            if column not in columns:
                db.execute(f'ALTER TABLE preference_update_receipts_v1 ADD COLUMN {column} TEXT')
        db.execute('CREATE INDEX IF NOT EXISTS preference_receipt_slot_v1 ON preference_update_receipts_v1 '
            '(processor,state,slot,slot_value,checked_at)')
        db.execute('CREATE INDEX IF NOT EXISTS preference_review_slot_v1 ON preference_update_receipts_v1 '
            '(state,slot,checked_at)')

    def _settings(self, db: sqlite3.Connection) -> PreferenceUpdateSettings:
        tier, whitelist = (ConsolidationWorkflow._settings(db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='consolidation_settings_v1'").fetchone()
            else ('review', {'profile_categories': set(PREFERENCE_CATEGORIES)}))
        available = whitelist['profile_categories']
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='preference_updates_settings_v1'").fetchone():
            return PreferenceUpdateSettings(tier == 'auto', tuple(item for item in PREFERENCE_CATEGORIES if item in available),
                None, self.processor.processor_version)
        row = db.execute('SELECT * FROM preference_updates_settings_v1 WHERE id=1').fetchone()
        return (PreferenceUpdateSettings(tier == 'auto', tuple(item for item in PREFERENCE_CATEGORIES if item in available),
                None, self.processor.processor_version) if row is None
            else PreferenceUpdateSettings(tier == 'auto', tuple(item for item in json.loads(row['categories']) if item in available),
                row['admitted_processor'], self.processor.processor_version))

    def settings(self, access: Access) -> PreferenceUpdateSettings:
        self.runtime._require_scope(access, 'maintain', 'preference_update_settings')
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            return self._settings(db)

    def status(self, access: Access) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'preference_update_status')
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            return self._status(db)

    def configure(self, access: Access, *, enabled: bool, categories: tuple[str, ...] = PREFERENCE_CATEGORIES,
                  admitted_processor: str | None = None) -> PreferenceUpdateSettings:
        self.runtime._require_scope(access, 'maintain', 'preference_update_configure')
        if (type(enabled) is not bool or not isinstance(categories, tuple)
                or any(not isinstance(category, str) or category not in PREFERENCE_CATEGORIES for category in categories)
                or len(categories) != len(set(categories))
                or (admitted_processor is not None and admitted_processor != self.processor.processor_version)):
            raise MemoryFailure('invalid_preference_update_config')
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            db.execute('INSERT INTO preference_updates_settings_v1 VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                'categories=excluded.categories,admitted_processor=excluded.admitted_processor', (json.dumps(categories), admitted_processor))
            ConsolidationWorkflow._schema(db)
            current_tier, whitelist = ConsolidationWorkflow._settings(db)
            whitelist['profile_categories'] = set(categories)
            db.execute('INSERT INTO consolidation_settings_v1 VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET '
                'tier=excluded.tier,whitelist=excluded.whitelist',
                ('auto' if enabled else 'review' if current_tier == 'auto' else current_tier,
                    _json({key: sorted(value) for key, value in whitelist.items()})))
            db.execute('COMMIT')
        return self.settings(access)

    def _pending(self, db: sqlite3.Connection, limit: int) -> tuple[str, ...]:
        settings = self._settings(db)
        if (not settings.enabled or settings.admitted_processor != settings.rules_processor or not settings.categories
                or not db.execute("SELECT 1 FROM sqlite_master WHERE name='preference_update_receipts_v1'").fetchone()
                or not db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_candidates_v1'").fetchone()):
            return ()
        placeholders = ','.join('?' for _ in settings.categories)
        return tuple(str(row[0]) for row in db.execute(f'''SELECT r.candidate_id FROM preference_update_receipts_v1 r
            JOIN fact_candidates_v1 c ON c.id=r.candidate_id WHERE r.state='draft' AND r.processor=?
            AND json_extract(c.draft,'$.preference_category') IN ({placeholders})
            ORDER BY r.checked_at,r.candidate_id LIMIT ?''', (settings.rules_processor, *settings.categories, limit)))

    def _status(self, db: sqlite3.Connection) -> dict[str, object]:
        counts = ({str(row[0]): int(row[1]) for row in db.execute(
            'SELECT state,count(*) FROM preference_update_receipts_v1 GROUP BY state')}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='preference_update_receipts_v1'").fetchone() else {})
        return {'settings': asdict(self._settings(db)), 'historical_receipts': counts,
            'pending_drafts': counts.get('draft', 0), 'queued_for_recheck_up_to_256': len(self._pending(db, 256))}

    def process(self, access: Access, source_ids: tuple[str, ...], *, timeout: float = 120,
                cancel: threading.Event | None = None, scheduler_token: str | None = None) -> PreferenceUpdateReport:
        started = time.monotonic()
        self.runtime._require_scope(access, 'accept', 'preference_update')
        self.runtime._require_scope(access, 'maintain', 'preference_update')
        if (not isinstance(source_ids, tuple) or not 1 <= len(source_ids) <= 16
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise MemoryFailure('invalid_extraction_request')
        for source_id in source_ids:
            _identifier(source_id)
        ids = tuple(dict.fromkeys(source_ids))
        deadline = started + timeout
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            sources = CandidateWorkflow._sources(db, ids)
            if sum(len(source.message.event.text) for source in sources) > 16384:
                raise MemoryFailure('extraction_input_not_admitted')
        runs: list[CandidateRun] = []
        updates: list[PreferenceUpdate] = []
        error_code = None
        # A coworker's invalidation must never erase this source's preference or denial.
        for source_id in ids:
            try:
                remaining = _remaining(deadline, cancel)
            except MemoryFailure as error:
                error_code = error.code
                break
            report = self.processor.process(access, (source_id,), timeout=remaining, cancel=cancel)
            runs.append(report.run)
            if report.error_code is not None:
                error_code = report.error_code
            if report.run.state == 'succeeded':
                for candidate in CandidateWorkflow(self.runtime).candidates(access, report.run.run_id):
                    try:
                        updates.append(self.apply(access, candidate.candidate_id, scheduler_token=scheduler_token,
                            deadline=deadline, cancel=cancel))
                    except MemoryFailure as error:
                        if error.code not in {'extraction_timeout', 'extraction_canceled'}:
                            raise
                        return PreferenceUpdateReport(tuple(runs), tuple(updates), error.code)
        return PreferenceUpdateReport(tuple(runs), tuple(updates), error_code)

    @staticmethod
    def _key_value(text: str, category: str) -> tuple[str, str]:
        _, negative, positive = _statement(text)
        predicate, value = PreferenceUpdates._positive_key_value(positive, category)
        return predicate, 'not:' + value if negative else value

    @staticmethod
    def _positive_key_value(text: str, category: str) -> tuple[str, str]:
        if category == 'response_style':
            if '简短' in text:
                return 'preference:answer_verbosity', 'brief'
            if '详细' in text:
                return 'preference:answer_verbosity', 'detailed'
            for word, value in (('先给结论再分点', 'conclusion_points'), ('表格', 'table'), ('分点', 'points'),
                                ('分段', 'paragraphs'), ('列表', 'list')):
                if word in text:
                    return 'preference:answer_layout', value
            raise MemoryFailure('preference_candidate_required')
        value = re.sub(r'^我(?:常用|习惯用|偏好用|喜欢|爱好)', '', text.strip()).rstrip('。.!！').strip().casefold()
        slot = value
        if category == 'technical_tool':
            tool = re.fullmatch(r'(.+?)(?:\s*([0-9]+(?:\.[0-9]+){0,2}))?', value)
            assert tool is not None
            slot = tool[1].strip()
            if slot == 'visual studio code':
                slot = 'vs code'
            value = slot + (':' + tool[2] if tool[2] is not None else '')
        return 'preference:' + category + ':' + hashlib.sha256(slot.encode()).hexdigest()[:32], value

    @staticmethod
    def _conflicts(left: str, right: str, category: str) -> bool:
        if left == right:
            return False
        if not left.startswith('not:') and not right.startswith('not:'):
            return True
        if left.startswith('not:') and right.startswith('not:'):
            return False
        negative, positive = (left[4:], right) if left.startswith('not:') else (right[4:], left)
        return negative == positive or category == 'technical_tool' and ':' not in negative

    def _tag(self, predicate: str) -> str:
        return 'auto-pref:' + hmac.digest(self.runtime._fingerprint_key, predicate.encode(), hashlib.sha256).hex()

    def apply(self, access: Access, candidate_id: str, *, scheduler_token: str | None = None,
              deadline: float | None = None, cancel: threading.Event | None = None) -> PreferenceUpdate:
        self.runtime._require_scope(access, 'accept', 'preference_update')
        self.runtime._require_scope(access, 'maintain', 'preference_update')
        facts, profiles = FactRegistry(self.runtime), ProfileRegistry(self.runtime)
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            def check_budget() -> None:
                if deadline is not None:
                    try:
                        _remaining(deadline, cancel)
                    except MemoryFailure as error:
                        if scheduler_token is not None and error.code == 'extraction_timeout':
                            raise MemoryFailure('scheduler_time_budget') from error
                        raise
            check_budget()
            if scheduler_token is not None:
                ConsolidationWorkflow._scheduler_fence(db, scheduler_token)
            draft, processor = facts._candidate(db, candidate_id)
            def finish(result: PreferenceUpdate) -> PreferenceUpdate:
                if scheduler_token is not None:
                    ConsolidationWorkflow._scheduler_fence(db, scheduler_token)
                self._schema(db)
                slot, slot_value = (self._key_value(draft.text, draft.preference_category)
                    if draft.preference_category is not None and self.classify_statement(draft.text) == draft.preference_category else (None, None))
                db.execute('INSERT INTO preference_update_receipts_v1 (candidate_id,processor,state,receipt,checked_at,slot,slot_value) '
                    'VALUES (?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET '
                    'state=excluded.state,receipt=excluded.receipt,checked_at=excluded.checked_at,slot=excluded.slot,slot_value=excluded.slot_value',
                    (candidate_id, processor, result.state, _json(asdict(result)), time.time(), slot, slot_value))
                check_budget()
                db.execute('COMMIT')
                return result
            category = draft.preference_category
            settings = self._settings(db)
            reason = ('automatic_disabled' if not settings.enabled else 'category_disabled' if category not in settings.categories
                else 'rules_not_admitted' if processor != settings.admitted_processor or processor != settings.rules_processor else None)
            if reason is not None:
                return finish(PreferenceUpdate(candidate_id, 'draft', reason))
            if category is None or self.classify_statement(draft.text) != category or draft.disputed or len(draft.sources) != 1:
                return finish(PreferenceUpdate(candidate_id, 'draft', 'preference_candidate_required'))
            span, = draft.sources
            source, = facts.workflow._sources(db, (span.message_id,))
            if (span.start != 0 or span.end != len(source.message.event.text) or draft.text != source.message.event.text
                    or not source.message.receipt.original_saved or source.message.event.completion != 'complete'):
                return finish(PreferenceUpdate(candidate_id, 'draft', 'preference_candidate_required'))
            if draft.text.strip().startswith('我希望'):
                return finish(PreferenceUpdate(candidate_id, 'draft', 'preference_scope_unclear'))
            if (source.message.event.occurred_at is not None
                    and _time(source.message.event.occurred_at) > _time(source.message.receipt.received_at)):
                return finish(PreferenceUpdate(candidate_id, 'draft', 'future_source'))
            predicate, value = self._key_value(draft.text, category)
            tag_id = self._tag(predicate)
            view = facts._query_snapshot(db, 'user', predicate)
            previous = profiles._latest(db, tag_id)
            if previous is not None:
                current = profiles._view(db, tag_id)
                if (current.state == 'confirmed' and view.fact is not None and not view.clarification_required
                        and self._key_value(view.fact.text, category)[1] == value
                        and previous.draft.sources == view.fact.sources):
                    return finish(PreferenceUpdate(candidate_id, 'merged', None, tag_id, predicate=predicate))
                resolved_id = self._resolved_statement(db, draft, predicate, tag_id)
                if resolved_id is not None:
                    return finish(PreferenceUpdate(candidate_id, 'resolved', 'preference_statement_already_reviewed', tag_id,
                        resolved_id, predicate))
                if current.state == 'draft' and previous.draft.processor_version.startswith(_LEGACY_REVIEW_PREFIX):
                    return finish(PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', tag_id,
                        previous.draft.processor_version.removeprefix(_LEGACY_REVIEW_PREFIX), predicate))
                if view.clarification_required:
                    pending = next((record for record in view.disputes if record.kind == 'disputed'), None)
                    return finish(PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', tag_id,
                        None if pending is None else pending.fact_id, predicate))
                if (current.state == 'confirmed' and view.fact is not None and current.record is not None
                        and current.record.draft.sources == view.fact.sources):
                    if not self._conflicts(self._key_value(view.fact.text, category)[1], value, category):
                        return finish(PreferenceUpdate(candidate_id, 'draft', 'compatible_preference_not_merged', tag_id, predicate=predicate))
                    opened = PreferenceClarifications(self.runtime)._open(db, OpenClarification('user', predicate,
                        tag_id, candidate_id, span.message_id, view.knowledge_revision, current.revision))
                    if scheduler_token is not None:
                        ConsolidationWorkflow._scheduler_fence(db, scheduler_token)
                    return finish(PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', tag_id,
                        opened.clarification_id, predicate))
                if (current.state == 'confirmed' and view.fact is None and self.classify_statement(previous.draft.label) == category
                        and self._key_value(previous.draft.label, category)[1] == value):
                    return finish(PreferenceUpdate(candidate_id, 'merged', 'already_user_confirmed', tag_id))
                return finish(PreferenceUpdate(candidate_id, 'draft', 'preference_review_required', tag_id, predicate=predicate))
            legacy = self._legacy_profile(db, candidate_id, draft, category, predicate, value, view)
            if legacy is not None:
                return finish(legacy)
            if view.knowledge_revision:
                return finish(PreferenceUpdate(candidate_id, 'draft', 'preference_review_required', tag_id, predicate=predicate))
            pending_review = self._pending_conflict(db, candidate_id, draft, predicate, value, tag_id)
            if pending_review is not None:
                return finish(pending_review)
            if _statement(draft.text)[1]:
                return finish(PreferenceUpdate(candidate_id, 'draft', 'negative_preference_requires_review', predicate=predicate))
            request_id = 'auto-pref-fact:' + hmac.digest(self.runtime._fingerprint_key,
                _json([predicate, candidate_id]).encode(), hashlib.sha256).hex()
            facts._decide(db, FactDecision(request_id, 'user', predicate, candidate_id, span.message_id, 0, 'assert',
                valid_from=source.message.event.occurred_at or source.message.receipt.received_at))
            proposal = profiles._decide(db, ProfileDecision('auto-pref-propose:' + request_id.rsplit(':', 1)[1],
                tag_id, 0, 'propose', ProfileDraft('interest' if category == 'general_interest' else 'preference',
                    draft.text, 1.0, draft.sources, processor)))
            profiles._decide(db, ProfileDecision('auto-pref-confirm:' + request_id.rsplit(':', 1)[1], tag_id,
                proposal.revision, 'auto_preference'))
            if scheduler_token is not None:
                ConsolidationWorkflow._scheduler_fence(db, scheduler_token)
            return finish(PreferenceUpdate(candidate_id, 'merged', None, tag_id, predicate=predicate))

    def _pending_conflict(self, db: sqlite3.Connection, candidate_id: str, draft: CandidateDraft,
                          predicate: str, value: str, tag_id: str) -> PreferenceUpdate | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='preference_update_receipts_v1'").fetchone():
            return None
        rows = db.execute('''SELECT candidate_id FROM preference_update_receipts_v1 WHERE state='draft'
            AND processor LIKE ? AND slot=? AND slot_value<>? AND candidate_id<>? ORDER BY checked_at LIMIT 1001''',
            (AUTOMATIC_PREFERENCE_PROCESSOR_PREFIX + '%', predicate, value, candidate_id)).fetchall()
        if len(rows) > 1000:
            return PreferenceUpdate(candidate_id, 'draft', 'candidate_scan_limit', predicate=predicate)
        facts = FactRegistry(self.runtime)
        for row in rows:
            try:
                other, _ = facts._candidate(db, str(row[0]))
            except MemoryFailure as error:
                if error.code not in {'stale_source', 'candidate_not_found'}:
                    raise
                continue
            if len(other.sources) != 1 or other.preference_category != draft.preference_category:
                continue
            if self.classify_statement(other.text) != draft.preference_category:
                continue
            if other.text.strip().startswith('我希望'):
                continue
            if (draft.preference_category is None
                    or not self._conflicts(self._key_value(other.text, draft.preference_category)[1], value, draft.preference_category)):
                continue
            prior, = facts.workflow._sources(db, (other.sources[0].message_id,))
            if (other.sources[0].start != 0 or other.text != prior.message.event.text
                    or prior.message.event.occurred_at is not None
                        and _time(prior.message.event.occurred_at) > _time(prior.message.receipt.received_at)):
                continue
            review_id = 'pending-preference:' + hmac.digest(self.runtime._fingerprint_key,
                _json([predicate, sorted((candidate_id, str(row[0])))]).encode(), hashlib.sha256).hex()
            ProfileRegistry(self.runtime)._decide(db, ProfileDecision(review_id, tag_id, 0, 'propose',
                ProfileDraft('interest' if draft.preference_category == 'general_interest' else 'preference',
                    draft.text, 1.0, (*draft.sources, *other.sources), _LEGACY_REVIEW_PREFIX + review_id)))
            return PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', tag_id, review_id, predicate)
        return None

    def _resolved_statement(self, db: sqlite3.Connection, draft: CandidateDraft, predicate: str, tag_id: str) -> str | None:
        rows = FactRegistry(self.runtime)._rows(db, 'user', predicate)
        resolved = {identifier for row in rows for identifier in row.resolves}
        prior = next((row.fact_id for row in rows if row.kind == 'disputed' and row.fact_id in resolved
            and row.sources == draft.sources), None)
        if prior is not None:
            return prior
        reviewed = db.execute('''SELECT request_id,json_extract(record,'$.confirmation_id') AS confirmation,
            json_extract(record,'$.confirmation_version') AS version FROM profile_events_v1 WHERE tag_id=?
            AND json_extract(record,'$.state') IN ('confirmed','rejected')
            AND json_extract(record,'$.confirmation_id') IS NOT NULL ORDER BY revision DESC LIMIT 1''', (tag_id,)).fetchone()
        if reviewed is None:
            return None
        state = self.runtime._state(db, reviewed['confirmation'])
        if state.status != 'active' or state.version != reviewed['version']:
            return None
        confirmation = db.execute('SELECT received FROM messages WHERE id=?', (reviewed['confirmation'],)).fetchone()
        source = db.execute('SELECT received FROM messages WHERE id=?', (draft.sources[0].message_id,)).fetchone()
        return str(reviewed['request_id']) if _time(source[0]) <= _time(confirmation[0]) else None

    def _legacy_profile(self, db: sqlite3.Connection, candidate_id: str, draft: CandidateDraft, category: str,
                        predicate: str, value: str, view: FactView) -> PreferenceUpdate | None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
            return None
        rows = db.execute('''SELECT e.record FROM profile_events_v1 e JOIN (
            SELECT tag_id,max(revision) AS revision FROM profile_events_v1 GROUP BY tag_id) h
            ON e.tag_id=h.tag_id AND e.revision=h.revision ORDER BY e.tag_id LIMIT 1001''').fetchall()
        if len(rows) > 1000:
            return PreferenceUpdate(candidate_id, 'draft', 'profile_scan_limit')
        matches = []
        unclassified = False
        for row in rows:
            record = ProfileRegistry._record(row[0])
            if record.draft.category not in {'preference', 'interest'}:
                continue
            classified = self.classify_statement(record.draft.label)
            if classified is None:
                unclassified |= record.draft.category == 'preference'
                continue
            if self._key_value(record.draft.label, classified)[0] == predicate:
                matches.append(record)
        if not matches:
            return PreferenceUpdate(candidate_id, 'draft', 'existing_profile_requires_review') if unclassified and category != 'general_interest' else None
        if len(matches) != 1:
            return PreferenceUpdate(candidate_id, 'draft', 'existing_profile_conflict')
        record, = matches
        profile = ProfileRegistry(self.runtime)._view(db, record.tag_id)
        if profile.state == 'confirmed' and self._key_value(record.draft.label, category)[1] == value:
            return PreferenceUpdate(candidate_id, 'merged', 'already_user_confirmed', record.tag_id)
        resolved_id = self._resolved_statement(db, draft, predicate, record.tag_id)
        if resolved_id is not None:
            return PreferenceUpdate(candidate_id, 'resolved', 'preference_statement_already_reviewed', record.tag_id, resolved_id)
        if profile.state == 'draft' and record.draft.processor_version.startswith(_LEGACY_REVIEW_PREFIX):
            return PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', record.tag_id,
                record.draft.processor_version.removeprefix(_LEGACY_REVIEW_PREFIX))
        if profile.state == 'draft' and record.draft.processor_version.startswith(PREFERENCE_CLARIFICATION_PROCESSOR):
            return PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', record.tag_id,
                record.draft.processor_version.removeprefix(PREFERENCE_CLARIFICATION_PROCESSOR), predicate)
        if (profile.state != 'confirmed' or profile.record is None
                or record.draft.category != ('interest' if category == 'general_interest' else 'preference')):
            return PreferenceUpdate(candidate_id, 'draft', 'preference_review_required', record.tag_id)
        if not self._conflicts(self._key_value(record.draft.label, category)[1], value, category):
            return PreferenceUpdate(candidate_id, 'draft', 'compatible_preference_not_merged', record.tag_id)
        if view.fact is not None and view.fact.sources == record.draft.sources:
            opened = PreferenceClarifications(self.runtime)._open(db, OpenClarification('user', predicate,
                record.tag_id, candidate_id, draft.sources[0].message_id, view.knowledge_revision, profile.revision))
            return PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', record.tag_id,
                opened.clarification_id, predicate)
        review_id = 'legacy-preference:' + hmac.digest(self.runtime._fingerprint_key,
            _json([record.tag_id, record.revision, candidate_id]).encode(), hashlib.sha256).hex()
        ProfileRegistry(self.runtime)._decide(db, ProfileDecision(review_id, record.tag_id, record.revision, 'revise',
            ProfileDraft(record.draft.category, draft.text, 1.0, draft.sources, _LEGACY_REVIEW_PREFIX + review_id)))
        return PreferenceUpdate(candidate_id, 'pending_clarification', 'preference_clarification_required', record.tag_id, review_id)

    def validate_publication(self, db: sqlite3.Connection, draft: ProfileDraft, tag_id: str) -> None:
        """Recheck the policy at the profile write boundary, including direct trusted API calls."""
        settings = self._settings(db)
        category = self.classify_statement(draft.label)
        if (not settings.enabled or category not in settings.categories or category is None
                or _statement(draft.label)[1]
                or draft.label.strip().startswith('我希望')
                or settings.admitted_processor != settings.rules_processor or draft.processor_version != settings.rules_processor
                or draft.category != ('interest' if category == 'general_interest' else 'preference') or len(draft.sources) != 1):
            raise MemoryFailure('preference_policy_not_admitted')
        span, = draft.sources
        source, = CandidateWorkflow._sources(db, (span.message_id,))
        if (source.message.event.role != 'user' or source.message.event.completion != 'complete' or span.start != 0
                or span.end != len(source.message.event.text) or draft.label != source.message.event.text
                or source.message.event.occurred_at is not None
                    and _time(source.message.event.occurred_at) > _time(source.message.receipt.received_at)):
            raise MemoryFailure('preference_candidate_required')
        predicate, _ = self._key_value(draft.label, category)
        view = FactRegistry._query_snapshot(db, 'user', predicate)
        if (tag_id != self._tag(predicate) or view.clarification_required or view.fact is None
                or view.fact.sources != draft.sources or view.fact.text != draft.label
                or view.fact.processor_version != settings.rules_processor):
            raise MemoryFailure('preference_review_required')
