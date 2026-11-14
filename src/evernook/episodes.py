"""Review-only cross-session excerpts; literal mentions are not entity identity or inferred facts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .candidates import EvidenceSpan, ExtractionSource
from .consolidation import ConsolidationDraft, ConsolidationWorkflow
from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json
from .safety import credential_signal


@dataclass(frozen=True)
class EpisodeView:
    candidate_id: str
    run_id: str
    state: str
    summary: str | None
    source_contexts: tuple[dict[str, Any], ...]
    entity_mentions: tuple[dict[str, Any], ...]
    independent_sources: int
    authority: str = 'review_candidate'
    summary_mode: str = 'source_excerpts'
    inference: bool = False


def episode_draft(sources: tuple[ExtractionSource, ...], *, topic: str = '跨会话对话摘录',
                  entities: tuple[str, ...] = ()) -> ConsolidationDraft:
    if (not isinstance(topic, str) or not 0 < len(topic) <= 128 or credential_signal(topic)
            or not isinstance(entities, tuple) or len(entities) > 8
            or any(not isinstance(name, str) or not name.strip() or len(name) > 64 or credential_signal(name) for name in entities)):
        raise MemoryFailure('invalid_episode')
    if (not 2 <= len(sources) <= 16 or sum(len(item.message.event.text) for item in sources) > 16384
            or len({(item.message.event.source.entry, item.message.event.source.session) for item in sources}) < 2
            or any(item.message.event.role not in {'user', 'assistant', 'tool'} or item.message.event.completion != 'complete'
                   or not item.message.receipt.original_saved or not item.message.event.text.strip() for item in sources)):
        raise MemoryFailure('episode_sources_incomplete')
    if any(not any(name in source.message.event.text for source in sources) for name in entities):
        raise MemoryFailure('episode_entity_not_grounded')
    spans = tuple(EvidenceSpan(item.message.receipt.message_id, item.state_version, 'original',
        item.message.receipt.content_version, 0, len(item.message.event.text), item.message.event.text) for item in sources)
    detail = _json({'format': 'episode-excerpts-v1', 'entities': list(dict.fromkeys(entities)),
        'quotes': [{'source_index': index, 'excerpt': source.message.event.text[:64],
                    'omitted_chars': max(0, len(source.message.event.text) - 64)} for index, source in enumerate(sources)]})
    if len(detail) > 3072:
        raise MemoryFailure('episode_summary_budget')
    return ConsolidationDraft('episode', topic, spans, detail, 'episodic_dialogue', 0.0)


class Episodes:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime
        self.workflow = ConsolidationWorkflow(runtime)

    def enabled(self, access: Access) -> bool:
        self.runtime._require_scope(access, 'maintain', 'episode_status')
        with self.runtime._connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='episode_policy_v1'").fetchone():
                return False
            row = db.execute('SELECT enabled FROM episode_policy_v1 WHERE id=1').fetchone()
            return bool(row and row[0])

    def configure(self, access: Access, *, enabled: bool) -> dict[str, object]:
        self.runtime._require_scope(access, 'maintain', 'episode_configure')
        self.runtime._require_scope(access, 'accept', 'episode_configure')
        if type(enabled) is not bool:
            raise MemoryFailure('invalid_episode_policy')
        with self.runtime._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS episode_policy_v1 (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL)')
            db.execute('INSERT INTO episode_policy_v1 VALUES (1,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled', (int(enabled),))
        return {'enabled': enabled, 'publication': 'review_only'}

    def propose(self, access: Access, source_ids: tuple[str, ...], *, topic: str = '跨会话对话摘录',
                entities: tuple[str, ...] = (), timeout: float = 10, cancel: threading.Event | None = None) -> EpisodeView:
        self.runtime._require_scope(access, 'maintain', 'episode_propose')
        self.runtime._require_scope(access, 'accept', 'episode_propose')
        if not isinstance(source_ids, tuple) or not 2 <= len(source_ids) <= 16 or type(timeout) not in (float, int) or not 0 < timeout <= 120:
            raise MemoryFailure('invalid_episode')
        deadline = time.monotonic() + timeout
        def budget() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('episode_canceled')
            left = deadline - time.monotonic()
            if left <= 0:
                raise MemoryFailure('episode_timeout')
            return left
        budget()
        identity = hashlib.sha256(Path(__file__).read_bytes() + _json([topic, entities]).encode()).hexdigest()
        lease = self.workflow.begin(access, tuple(dict.fromkeys(source_ids)), processor_version='episode-excerpts-v1:' + identity)
        if lease.token is None:
            budget()
            result = self.workflow.result(access, lease.status.run_id)
        else:
            try:
                draft = episode_draft(lease.sources, topic=topic, entities=entities)
                budget()
                result = self.workflow.complete(access, lease.status.run_id, lease.token, (draft,), check=budget)
            except Exception:
                # Preserve the original error if the lease was concurrently revoked or expired.
                try:
                    self.workflow.fail(access, lease.status.run_id, lease.token, error_code='input_not_admitted')
                except MemoryFailure:
                    pass
                raise
        budget()
        return self.read(access, result.drafts[0].candidate_id)

    def read(self, access: Access, candidate_id: str) -> EpisodeView:
        self.runtime._require_scope(access, 'maintain', 'episode_read')
        self.runtime._require_scope(access, 'recall', 'episode_read')
        _identifier(candidate_id)
        with self.runtime._connection() as db:
            db.execute('BEGIN')
            return self._view(db, candidate_id)

    def _view(self, db: sqlite3.Connection, candidate_id: str) -> EpisodeView:
        _identifier(candidate_id)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='consolidation_drafts_v1'").fetchone():
            raise MemoryFailure('episode_not_found')
        row = db.execute("SELECT * FROM consolidation_drafts_v1 WHERE candidate_id=? AND kind='episode'", (candidate_id,)).fetchone()
        if row is None:
            raise MemoryFailure('episode_not_found')
        draft = self.workflow._draft_row(row)
        if not self.workflow._available_draft(db, draft):
            return EpisodeView(candidate_id, draft.run_id, 'unavailable', None, (), (), 0)
        try:
            detail = json.loads(draft.detail)
            if detail['format'] != 'episode-excerpts-v1' or not isinstance(detail['entities'], list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise MemoryFailure('invalid_episode') from None
        sources = self.workflow.candidates._sources(db, tuple(span.message_id for span in draft.sources))
        # Generic consolidation can also write this kind. Reconstruct the canonical
        # excerpt payload so a valid source ID cannot legitimize invented summary text.
        expected = episode_draft(sources, topic=draft.label, entities=tuple(detail['entities']))
        if expected.sources != draft.sources or expected.detail != draft.detail or expected.category != draft.category:
            raise MemoryFailure('invalid_episode')
        contexts, mentions = [], []
        origins = set()
        for span, source in zip(draft.sources, sources):
            event, receipt = source.message.event, source.message.receipt
            origins.add((event.source.entry, event.source.session, event.source.branch, event.source.event))
            contexts.append({'source_id': receipt.message_id, 'origin': asdict(event.source), 'role': event.role,
                'evidence_kind': {'user': 'user_statement', 'assistant': 'assistant_conversation', 'tool': 'external_material'}[event.role],
                'occurred_at': event.occurred_at, 'learned_at': receipt.received_at, 'state_version': source.state_version,
                'content_version': receipt.content_version, 'start': span.start, 'end': span.end,
                'quote': span.expected_text})
            for name in detail['entities']:
                if not isinstance(name, str):
                    raise MemoryFailure('invalid_episode')
                position = event.text.find(name)
                if position >= 0:
                    mentions.append({'name': name, 'source_id': receipt.message_id, 'start': position,
                        'end': position + len(name), 'meaning': 'literal_mention_not_identity'})
        return EpisodeView(candidate_id, draft.run_id, draft.state, draft.detail, tuple(contexts), tuple(mentions), len(origins))
