"""Bounded source excerpts and published observations; no free generation in normal views."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import secrets
import threading
import time
from typing import Any

from .automatic_preferences import PreferenceUpdates
from .candidates import EvidenceSpan
from .models import Access, MemoryFailure
from .observations import ObservationRegistry, source_contexts
from .preference_statements import assess_preferences
from .runtime import _json, _time
from .topic_views import TopicView, TopicViewDefinition, TopicViews


def view_budget(timeout: float, cancel: threading.Event | None) -> Callable[[], float]:
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise MemoryFailure('invalid_topic_view_budget')
    deadline = time.monotonic() + timeout
    def remaining() -> float:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('topic_view_canceled')
        left = deadline - time.monotonic()
        if left <= 0:
            raise MemoryFailure('topic_view_timeout')
        return left
    return remaining


def _in_scope(definition: TopicViewDefinition, context: dict[str, Any]) -> bool:
    origin = context['origin']
    return (not definition.entries or origin['entry'] in definition.entries) and (
        not definition.sessions or (origin['entry'], origin['session']) in definition.sessions) and (
        not definition.source_ids or context['message_id'] in definition.source_ids) and (
        definition.since is None or context['occurred_at'] is not None and _time(context['occurred_at']) >= _time(definition.since)) and (
        definition.until is None or context['occurred_at'] is not None and _time(context['occurred_at']) < _time(definition.until))


def build(views: TopicViews, access: Access, definition: TopicViewDefinition, edits: tuple[str, ...], *,
          budget: Callable[[], float], cancel: threading.Event | None) -> tuple[dict[str, Any], tuple[EvidenceSpan, ...], tuple[tuple[str, int], ...]]:
    page = views.recall.recall(access, definition.query, limit=64, since=definition.since, until=definition.until,
        timeout=budget(), cancel=cancel)
    data: dict[str, Any] = {'title': definition.title, 'purpose': definition.purpose, 'query': definition.query,
        'summary_mode': 'source_excerpts_and_reviewed_observations', 'inference': True,
        'raw_excerpts': [], 'observations': [], 'citations': [], 'conflicts': [],
        'coverage': {'recall_degraded': page.degraded, 'scope_filter_after_candidates': bool(definition.entries or definition.sessions or definition.source_ids),
            'raw_candidate_count': len(page.hits), 'at_candidate_limit': len(page.hits) >= 64,
            'omitted': [], 'generic_contradiction_detection': 'not_run'}}
    spans: list[EvidenceSpan] = []
    observation_refs: list[tuple[str, int]] = []
    chars = 0
    def admit(db_contexts: tuple[dict[str, Any], ...], proposed: tuple[EvidenceSpan, ...], extra_chars: int = 0, *, explicit_edit: bool = False) -> bool:
        nonlocal chars
        additions = tuple(span for span in proposed if span not in spans)
        new_chars = sum(len(span.expected_text) for span in additions) + extra_chars
        if (len({span.message_id for span in (*spans, *additions)}) > definition.max_sources
                or chars + new_chars > definition.max_chars or not explicit_edit and not all(_in_scope(definition, context) for context in db_contexts)):
            return False
        for span, context in zip(proposed, db_contexts):
            if span not in spans:
                spans.append(span)
                data['citations'].append(context)
        chars += new_chars
        return True
    with views.runtime._connection(check=budget) as db:
        db.execute('BEGIN')
        for key in definition.observation_keys:
            budget()
            related = ObservationRegistry(views.runtime)
            # Query policy is reused on this same source/observation read snapshot below.
            ids = db.execute('''SELECT e.observation_id FROM observation_events_v1 e WHERE e.topic_key=?
                AND e.state IN ('published','conflicted') AND NOT EXISTS (SELECT 1 FROM observation_events_v1 newer
                    WHERE newer.observation_id=e.observation_id AND newer.revision>e.revision) ORDER BY e.observation_id LIMIT 17''', (key,)).fetchall()
            if len(ids) > 16:
                data['coverage']['omitted'].append('observation_topic_page_limit:' + key)
            for row in ids[:16]:
                record = related._latest(db, row[0])
                if record is None:
                    continue
                observed = related._view(db, record)
                if observed.state not in {'published', 'conflicted'}:
                    continue
                if record.draft.valid_from is not None and _time(record.draft.valid_from) > time.time():
                    due = _time(record.draft.valid_from)
                    data['coverage']['refresh_not_later_than'] = min(due, data['coverage'].get('refresh_not_later_than', due))
                    continue
                if record.draft.valid_until is not None and _time(record.draft.valid_until) <= time.time():
                    continue
                if record.draft.valid_until is not None:
                    due = _time(record.draft.valid_until)
                    data['coverage']['refresh_not_later_than'] = min(due, data['coverage'].get('refresh_not_later_than', due))
                cited = (*record.draft.support, *record.draft.counterevidence)
                contexts = related._contexts(db, record.draft)
                if not admit(contexts, cited, len(observed.conclusion or '')):
                    data['coverage']['omitted'].append('observation_scope_or_budget:' + record.observation_id)
                    continue
                observation_refs.append((record.observation_id, record.revision))
                data['observations'].append({'observation_id': record.observation_id, 'revision': record.revision,
                    'state': observed.state, 'conclusion': observed.conclusion, 'inference': True,
                    'support': [asdict(span) for span in record.draft.support],
                    'counterevidence': [asdict(span) for span in record.draft.counterevidence]})
                if observed.state == 'conflicted':
                    data['conflicts'].append({'observation_id': record.observation_id, 'kind': 'reviewed_conflict'})
        slots: dict[str, list[tuple[str, str, str]]] = {}
        source_ids = list(dict.fromkeys((*edits, *definition.source_ids, *(hit.evidence.message_id for hit in page.hits))))
        for message_id in source_ids:
            budget()
            try:
                state = views.runtime._state(db, message_id)
                stored = views.runtime._stored(db.execute('SELECT event,receipt FROM messages WHERE id=?', (message_id,)).fetchone())
            except MemoryFailure as error:
                if error.code != 'message_not_found':
                    raise
                data['coverage']['omitted'].append('unavailable_source:' + message_id)
                continue
            text = stored.event.text
            if not text:
                continue
            matching = next((hit.evidence.spans[0].start for hit in page.hits
                if hit.evidence.message_id == message_id and hit.evidence.spans), 0)
            start = max(0, matching - 64)
            end = min(len(text), start + 512)
            span = EvidenceSpan(message_id, state.version, 'original', stored.receipt.content_version, start, end, text[start:end])
            try:
                contexts = source_contexts(db, (span,), 'raw_excerpt')
            except MemoryFailure as error:
                if error.code not in {'stale_observation_source', 'message_not_found', 'observation_span_mismatch'}:
                    raise
                continue
            explicit_edit = message_id in edits
            if explicit_edit:
                contexts[0]['scope_exception'] = 'explicit_user_view_edit'
            if not admit(contexts, (span,), explicit_edit=explicit_edit):
                data['coverage']['omitted'].append('raw_scope_or_budget:' + message_id)
                continue
            data['raw_excerpts'].append({'message_id': message_id, 'start': start, 'end': end,
                'bounded_excerpt': start != 0 or end != len(text)})
            if stored.event.role == 'user':
                for statement in assess_preferences(text).statements:
                    if statement.scope in {'long_term', 'withdrawal'}:
                        key, value = PreferenceUpdates._key_value(statement.text, statement.category)
                        slots.setdefault(key, []).append((value, statement.category, message_id))
        for key, values in slots.items():
            if any(PreferenceUpdates._conflicts(left[0], right[0], left[1]) for index, left in enumerate(values) for right in values[index + 1:]):
                data['conflicts'].append({'topic_key': key, 'kind': 'finite_preference_conflict', 'source_ids': sorted({value[2] for value in values})})
    budget()
    return data, tuple(spans), tuple(observation_refs)


def refresh(views: TopicViews, access: Access, view_id: str, *, timeout: float, cancel: threading.Event | None) -> TopicView:
    views.runtime._require_scope(access, 'maintain', 'topic_view_refresh')
    views.runtime._require_scope(access, 'recall', 'topic_view_refresh')
    remaining = view_budget(timeout, cancel)
    remaining()
    token = secrets.token_urlsafe(24)
    with views.runtime._connection(check=remaining) as db:
        db.execute('BEGIN IMMEDIATE')
        current = views._read(db, view_id)
        if current.state in {'valid', 'conflict', 'empty', 'paused', 'deleted'}:
            return current
        record = views._latest(db, view_id)
        assert record is not None and current.definition is not None
        job = db.execute('SELECT * FROM topic_view_jobs_v1 WHERE view_id=?', (view_id,)).fetchone()
        if job and job['state'] == 'running' and float(job['lease_until']) > time.time():
            raise MemoryFailure('topic_view_refresh_busy')
        clock = views._clock(db)
        target = _json([current.definition_revision, clock])
        db.execute('''INSERT INTO topic_view_jobs_v1 VALUES (?,'running',?,?,?,1,?,NULL)
            ON CONFLICT(view_id) DO UPDATE SET state='running',target=excluded.target,token=excluded.token,
            lease_until=excluded.lease_until,attempt=topic_view_jobs_v1.attempt+1,last_served=excluded.last_served,error=NULL''',
            (view_id, target, token, time.time() + remaining() + 5, time.time()))
        processor = views.processor
        db.execute('COMMIT')
    try:
        data, spans, observations = build(views, access, current.definition, tuple(record['edit_source_ids']), budget=remaining, cancel=cancel)
        remaining()
        with views.runtime._connection(check=remaining) as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM topic_view_jobs_v1 WHERE view_id=?', (view_id,)).fetchone()
            latest = views._latest(db, view_id)
            if job is None or job['token'] != token or float(job['lease_until'] or 0) <= time.time():
                raise MemoryFailure('topic_view_lease_expired')
            if (latest is None or latest['state'] != 'active' or latest['revision'] != current.definition_revision
                    or views._clock(db) != clock or views.processor != processor):
                db.execute("UPDATE topic_view_jobs_v1 SET state='pending',token=NULL,lease_until=NULL WHERE view_id=? AND token=?", (view_id, token))
                result = views._read(db, view_id)
                db.execute('COMMIT')
                return result
            source_contexts(db, spans, 'view_evidence')
            identity = views.runtime._authorize(access)
            if not {'maintain', 'recall'}.issubset(identity.scopes):
                raise MemoryFailure('permission_denied')
            for observation_id, revision in observations:
                observed = ObservationRegistry._latest(db, observation_id)
                if observed is None or observed.revision != revision or ObservationRegistry._view(db, observed).state not in {'published', 'conflicted'}:
                    raise MemoryFailure('stale_observation_source')
            # Attempts remain monotonic when source purge deletes old snapshot bodies.
            snapshot_revision = int(job['attempt'])
            now = datetime.now(timezone.utc)
            expiry = now + timedelta(seconds=current.definition.ttl_seconds)
            if data['coverage'].get('refresh_not_later_than') is not None:
                expiry = min(expiry, datetime.fromtimestamp(data['coverage']['refresh_not_later_than'], timezone.utc))
            state = 'empty' if not spans else 'conflict' if data['conflicts'] else 'valid'
            db.execute('INSERT INTO topic_view_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?)', (view_id, snapshot_revision,
                current.definition_revision, processor, _json(clock), now.isoformat(),
                expiry.isoformat(), state, _json(data),
                _json([asdict(span) for span in spans]), _json(observations)))
            db.execute("UPDATE topic_view_jobs_v1 SET state='complete',token=NULL,lease_until=NULL,error=NULL WHERE view_id=? AND token=?", (view_id, token))
            remaining()
            result = views._read(db, view_id)
            db.execute('COMMIT')
            return result
    except Exception as error:
        try:
            with views.runtime._connection() as db:
                db.execute("UPDATE topic_view_jobs_v1 SET state='pending',token=NULL,lease_until=NULL,error=? WHERE view_id=? AND token=?",
                    (error.code if isinstance(error, MemoryFailure) else 'topic_view_refresh_failed', view_id, token))
        except MemoryFailure:
            pass  # Preserve the original failure; an uncleared lease still expires.
        raise
