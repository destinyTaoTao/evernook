"""Versioned derived vectors and evidence fusion; no fact acceptance or query cache."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import heapq
import math
import sqlite3
import struct
import threading
import time

from .embedding import EmbeddingBackend, EmbeddingSpace, unit_vector
from .models import Access, LexicalStatus, MemoryFailure, SearchHit, SearchSpan
from .runtime import MemoryRuntime, _identifier, _json, _RECALLABLE, _time


_ELIGIBLE = f"{_RECALLABLE} AND json_extract(messages.receipt,'$.original_saved')=1 AND json_extract(messages.event,'$.text')<>''"


@dataclass(frozen=True)
class SemanticStatus:
    state: str
    space_id: str | None
    source_messages: int | None
    indexed_messages: int | None
    pending_messages: int | None
    source_watermark: str | None
    error_code: str | None = None
    failed_chunks: int | None = None


@dataclass(frozen=True)
class RecallHit:
    evidence: SearchHit
    channels: tuple[str, ...]
    score: float
    similarity: float | None


@dataclass(frozen=True)
class RecallPage:
    hits: tuple[RecallHit, ...]
    lexical: LexicalStatus
    semantic: SemanticStatus
    degraded: tuple[str, ...]
    evidence_state: str
    recent_since: str
    recent_expanded: bool
    recent_content_chars: int
    recent_window_limited: bool
    semantic_scanned_chunks: int
    semantic_scan_limited: bool


class SemanticRecall:
    def __init__(self, runtime: MemoryRuntime, backend: EmbeddingBackend | None = None):
        self.runtime = runtime
        self.backend = backend
        self.space = backend.space if backend is not None else None
        if self.space is not None:
            space = self.space
            if (type(space.dimensions) is not int or not 1 <= space.dimensions <= 4096
                    or type(space.chunk_size) is not int or not 32 <= space.chunk_size <= 256
                    or type(space.overlap) is not int or not 0 <= space.overlap < space.chunk_size
                    or any(not isinstance(value, str) or not value or len(value) > 256
                           for value in (space.model, space.revision, space.artifact_digest, space.encoding))):
                raise MemoryFailure("invalid_embedding_space")
            for value in (space.model, space.revision, space.artifact_digest, space.encoding):
                _identifier(value)

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS semantic_spaces_v1 (id TEXT PRIMARY KEY, specification TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS semantic_documents_v1 (
            space_id TEXT NOT NULL REFERENCES semantic_spaces_v1(id), message_id TEXT NOT NULL REFERENCES messages(id),
            state_version INTEGER NOT NULL, content_version INTEGER NOT NULL, text_length INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL, PRIMARY KEY(space_id,message_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS semantic_chunks_v1 (
            space_id TEXT NOT NULL REFERENCES semantic_spaces_v1(id), message_id TEXT NOT NULL REFERENCES messages(id),
            start INTEGER NOT NULL, end INTEGER NOT NULL, state_version INTEGER NOT NULL,
            content_version INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(space_id,message_id,start))""")
        db.execute("""CREATE TABLE IF NOT EXISTS semantic_failures_v1 (
            space_id TEXT NOT NULL REFERENCES semantic_spaces_v1(id), message_id TEXT NOT NULL REFERENCES messages(id),
            start INTEGER NOT NULL, state_version INTEGER NOT NULL, content_version INTEGER NOT NULL,
            error_code TEXT NOT NULL, PRIMARY KEY(space_id,message_id,start))""")

    @staticmethod
    def _present(db: sqlite3.Connection) -> bool:
        return bool(db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('semantic_spaces_v1','semantic_documents_v1','semantic_chunks_v1')").fetchone()[0] == 3)

    @staticmethod
    def _covered() -> str:
        # A chunk is acknowledged when it has a current-version vector or an explicit failure record.
        # The CASE guard keeps older stores without the failure table reporting their existing coverage.
        return f"""SELECT d.message_id FROM semantic_documents_v1 d
            LEFT JOIN visibility v ON v.message_id=d.message_id
            JOIN messages m ON m.id=d.message_id
            WHERE d.space_id=? AND d.state_version=COALESCE(v.version,1)
              AND d.content_version=json_extract(m.receipt,'$.content_version')
              AND d.chunk_count=(SELECT count(*) FROM semantic_chunks_v1 c
                  WHERE c.space_id=d.space_id AND c.message_id=d.message_id
                    AND c.state_version=d.state_version AND c.content_version=d.content_version)
                + CASE WHEN EXISTS (SELECT 1 FROM sqlite_master WHERE name='semantic_failures_v1')
                  THEN (SELECT count(*) FROM semantic_failures_v1 f
                      WHERE f.space_id=d.space_id AND f.message_id=d.message_id
                        AND f.state_version=d.state_version AND f.content_version=d.content_version) ELSE 0 END"""

    def _status(self, db: sqlite3.Connection) -> SemanticStatus:
        counts = db.execute(f"SELECT count(*),max(received) FROM messages WHERE {_ELIGIBLE}").fetchone()
        total = int(counts[0])
        space = self.space
        if space is None:
            return SemanticStatus("disabled", None, total, 0, total, counts[1])
        if not self._present(db):
            return SemanticStatus("not_built", space.key, total, 0, total, counts[1])
        covered = int(db.execute(f"SELECT count(*) FROM messages WHERE {_ELIGIBLE} AND id IN ({self._covered()})", (space.key,)).fetchone()[0])
        failure_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_failures_v1'").fetchone()
        failed = 0
        if failure_table:
            # Only failures that still describe a current recallable source version count as live.
            failed = int(db.execute(f"""SELECT count(*) FROM semantic_failures_v1 f
                JOIN messages ON messages.id=f.message_id LEFT JOIN visibility v ON v.message_id=f.message_id
                WHERE f.space_id=? AND {_ELIGIBLE} AND f.state_version=COALESCE(v.version,1)
                  AND f.content_version=json_extract(messages.receipt,'$.content_version')""", (space.key,)).fetchone()[0])
        return SemanticStatus("ready" if covered == total else "behind", space.key, total, covered, total - covered, counts[1],
                              None, failed)

    def status(self, access: Access) -> SemanticStatus:
        self.runtime._authorize(access)
        try:
            with self.runtime._connection() as db:
                db.execute("BEGIN")
                return self._status(db)
        except MemoryFailure as failure:
            if failure.code not in {"storage_busy", "storage_unavailable", "storage_full"}:
                raise
            return SemanticStatus("unavailable", self.space.key if self.space else None,
                                  None, None, None, None, failure.code)

    def refresh(self, access: Access, *, limit: int = 16, timeout: float = 30) -> SemanticStatus:
        self.runtime._authorize(access)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise MemoryFailure("invalid_page")
        self._timeout(timeout)
        backend, space = self.backend, self.space
        if backend is None or space is None:
            raise MemoryFailure("embedding_disabled")
        # Snapshot bounded missing chunks; no inference or waiting inside a DB transaction.
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            db.execute("INSERT OR IGNORE INTO semantic_spaces_v1 VALUES (?,?)", (space.key, _json(asdict(space))))
            if db.execute("SELECT specification FROM semantic_spaces_v1 WHERE id=?", (space.key,)).fetchone()[0] != _json(asdict(space)):
                raise MemoryFailure("embedding_space_mismatch")
            db.execute("COMMIT")
            db.execute("BEGIN")
            rows = db.execute(f"""SELECT messages.event,messages.receipt,COALESCE(v.version,1) AS state_version
                FROM messages LEFT JOIN visibility v ON v.message_id=messages.id
                WHERE {_ELIGIBLE} AND messages.id NOT IN ({self._covered()}) ORDER BY received,messages.id LIMIT ?""", (space.key, limit)).fetchall()
            pending: list[tuple[str, int, int, int, int, int, int, str]] = []
            for row in rows:
                message = self.runtime._stored(row)
                text, receipt, version = message.event.text, message.receipt, int(row["state_version"])
                existing = {int(item[0]) for item in db.execute("""SELECT start FROM semantic_chunks_v1
                    WHERE space_id=? AND message_id=? AND state_version=? AND content_version=?""",
                    (space.key, receipt.message_id, version, receipt.content_version))}
                failed = {int(item[0]) for item in db.execute("""SELECT start FROM semantic_failures_v1
                    WHERE space_id=? AND message_id=? AND state_version=? AND content_version=?""",
                    (space.key, receipt.message_id, version, receipt.content_version))}
                starts = range(0, max(1, len(text) - space.overlap), space.chunk_size - space.overlap)
                for start in starts:
                    if start in existing or start in failed:
                        continue
                    end = min(len(text), start + space.chunk_size)
                    pending.append((receipt.message_id, version, receipt.content_version, len(text), len(starts), start, end, text[start:end]))
                    if len(pending) == limit:
                        break
                if len(pending) == limit:
                    break
        if not pending:
            return self.status(access)
        vectors = backend.embed(tuple(item[-1] for item in pending), timeout=timeout)
        if len(vectors) != len(pending):
            raise MemoryFailure("invalid_embedding")
        encoded = [None if vector is None else struct.pack(f"<{space.dimensions}f", *unit_vector(vector, space.dimensions))
                   for vector in vectors]
        writable = [(item, code) for item, code in zip(pending, encoded) if code is not None]
        unembeddable = [item for item, code in zip(pending, encoded) if code is None]
        self.runtime._authorize(access)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for item, vector in writable:
                message_id, version, content_version, length, count, start, end, _ = item
                current = self.runtime._state(db, message_id)
                if current.status != "active" or current.version != version:
                    raise MemoryFailure("stale_source")
                db.execute("INSERT OR REPLACE INTO semantic_documents_v1 VALUES (?,?,?,?,?,?)",
                    (space.key, message_id, version, content_version, length, count))
                db.execute("INSERT OR REPLACE INTO semantic_chunks_v1 VALUES (?,?,?,?,?,?,?)",
                    (space.key, message_id, start, end, version, content_version, vector))
                db.execute("""DELETE FROM semantic_failures_v1 WHERE space_id=? AND message_id=?
                    AND (state_version<>? OR content_version<>?)""",
                    (space.key, message_id, version, content_version))
            db.execute("COMMIT")
        if unembeddable:
            self._mark_failures(access, unembeddable)
        return self.status(access)

    def _mark_failures(self, access: Access, items: list[tuple[str, int, int, int, int, int, int, str]]) -> None:
        """Record deterministic per-chunk model failures so one bad piece cannot wedge the queue."""
        self.runtime._authorize(access)
        assert self.space is not None
        space = self.space
        grouped: dict[tuple[str, int, int, int, int], list[int]] = {}
        for item in items:
            message_id, version, content_version, length, count, start, _, _ = item
            grouped.setdefault((message_id, version, content_version, length, count), []).append(start)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for (message_id, version, content_version, length, count), starts in grouped.items():
                current = self.runtime._state(db, message_id)
                if current.status != "active" or current.version != version:
                    continue  # A state/content change re-derives the chunk; the stale failure is not recorded.
                db.execute("INSERT OR REPLACE INTO semantic_documents_v1 VALUES (?,?,?,?,?,?)",
                    (space.key, message_id, version, content_version, length, count))
                db.execute("""DELETE FROM semantic_failures_v1 WHERE space_id=? AND message_id=?
                    AND (state_version<>? OR content_version<>?)""",
                    (space.key, message_id, version, content_version))
                for start in starts:
                    db.execute("INSERT OR REPLACE INTO semantic_failures_v1 VALUES (?,?,?,?,?,?)",
                        (space.key, message_id, start, version, content_version, "token_limit"))
            db.execute("COMMIT")

    @staticmethod
    def _timeout(value: float) -> None:
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 120:
            raise MemoryFailure("invalid_timeout")

    def _recent(self, access: Access, as_of: str, target_chars: int, days: int, *, check: Callable[[], float] | None = None) -> tuple[str, bool, int, bool]:
        self.runtime._authorize(access)
        instant = _time(as_of)
        base = instant - timedelta(days=days).total_seconds()
        since, chars, scanned = base, 0, 0
        # Unknown/future source times never pretend to be recent. They remain in global recall.
        with self.runtime._connection() as db:
            for row in db.execute(f"""SELECT event,receipt,occurred FROM messages WHERE {_ELIGIBLE}
                    AND occurred<=? ORDER BY occurred DESC,id LIMIT 257""", (instant,)):
                # The scan-limit return wins at the boundary: verified recent rows must not be dropped
                # to a deadline/cancel firing on exactly the row where the partial window is promised.
                if scanned == 256:
                    return datetime.fromtimestamp(since, timezone.utc).isoformat(), since < base, chars, True
                if check is not None:
                    check()
                scanned += 1
                chars += len(self.runtime._stored(row).event.text)
                since = min(since, float(row["occurred"]))
                if chars >= target_chars:
                    break
        return datetime.fromtimestamp(since, timezone.utc).isoformat(), since < base, chars, False

    def _semantic(self, access: Access, vector: tuple[float, ...], *, since: str | None, until: str | None,
                  minimum: float, check: Callable[[], float] | None = None) -> tuple[list[tuple[SearchHit, float]], int, bool]:
        space = self.space
        if space is None:
            return [], 0, False
        parameters: list[object] = [space.key]
        condition = ""
        for key, value, operator in (("since", since, ">="), ("until", until, "<")):
            if value is not None:
                condition += f" AND occurred{operator}?"
                parameters.append(_time(value))
        best: dict[str, tuple[SearchHit, float]] = {}
        scanned = 0
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            if not self._present(db):
                return [], 0, False
            rows = db.execute(f"""SELECT messages.event,messages.receipt,c.start,c.end,c.vector,
                COALESCE(v.version,1) AS state_version FROM semantic_chunks_v1 c
                JOIN messages ON messages.id=c.message_id LEFT JOIN visibility v ON v.message_id=messages.id
                WHERE c.space_id=? AND {_ELIGIBLE} AND c.state_version=COALESCE(v.version,1)
                  AND c.content_version=json_extract(messages.receipt,'$.content_version') {condition}
                ORDER BY received,messages.id,c.start LIMIT 10001""", parameters)
            for row in rows:
                # The scan-limit return wins at the boundary: fully verified chunk hits must not be
                # dropped to a deadline/cancel firing on exactly the row where partial results are promised.
                if scanned == 10000:
                    return heapq.nlargest(100, best.values(), key=lambda item: (item[1], item[0].message_id)), scanned, True
                if check is not None:
                    check()
                scanned += 1
                stored = self.runtime._stored(row)
                start, end = int(row["start"]), int(row["end"])
                if not 0 <= start < end <= len(stored.event.text) or end - start > space.chunk_size:
                    raise MemoryFailure("invalid_semantic_index")
                raw = bytes(row["vector"])
                if len(raw) != space.dimensions * 4:
                    raise MemoryFailure("invalid_semantic_index")
                other = unit_vector(struct.unpack(f"<{space.dimensions}f", raw), space.dimensions)
                similarity = sum(a * b for a, b in zip(vector, other))
                if similarity < minimum:
                    continue
                receipt, event = stored.receipt, stored.event
                hit = SearchHit(receipt.message_id, event.source, event.role, event.sequence, event.occurred_at,
                    receipt.received_at, event.completion, receipt.representation, receipt.content_version,
                    int(row["state_version"]), (SearchSpan(start, end, event.text[start:end]),))
                if hit.message_id not in best or similarity > best[hit.message_id][1]:
                    best[hit.message_id] = hit, similarity
        return heapq.nlargest(100, best.values(), key=lambda item: (item[1], item[0].message_id)), scanned, False

    def recall(self, access: Access, query: str, *, limit: int = 10, as_of: str | None = None,
               recent_days: int = 7, recent_target_chars: int = 8192, min_similarity: float = 0.4, timeout: float = 30,
               cancel: threading.Event | None = None) -> RecallPage:
        self.runtime._authorize(access)
        self._timeout(timeout)
        deadline = time.monotonic() + timeout
        def budget() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure("recall_canceled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise MemoryFailure("recall_timeout")
            return left
        budget()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryFailure("invalid_page")
        if type(recent_target_chars) is not int or not 1 <= recent_target_chars <= 1048576:
            raise MemoryFailure("invalid_recent_budget")
        if type(recent_days) is not int or not 1 <= recent_days <= 36500:
            raise MemoryFailure("invalid_recent_window")
        if type(min_similarity) not in (int, float) or not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1:
            raise MemoryFailure("invalid_similarity")
        # Core validation and protected-query refusal happen before any model invocation.
        lexical = self.runtime.search(access, query, limit=100)
        budget()
        exact = self.runtime.search(access, query, mode="exact", limit=100)
        instant = as_of if as_of is not None else datetime.now(timezone.utc).isoformat()
        since, expanded, chars, window_limited = self._recent(access, instant, recent_target_chars, recent_days, check=budget)
        # until is exclusive; one microsecond includes the reference instant in this channel.
        until = (datetime.fromtimestamp(_time(instant), timezone.utc) + timedelta(microseconds=1)).isoformat()
        recent_lexical = self.runtime.search(access, query, since=since, until=until, limit=100)
        channels: dict[str, list[SearchHit]] = {"exact": list(exact.hits), "lexical": list(lexical.hits),
                                               "recent_lexical": list(recent_lexical.hits)}
        degraded = [reason for reason in (lexical.fallback_reason, exact.fallback_reason) if reason is not None]
        similarities: dict[str, float] = {}
        scanned, scan_limited = 0, False
        semantic_status = self.status(access)
        if semantic_status.state != "ready":
            degraded.append("semantic_" + semantic_status.state)
        backend, space = self.backend, self.space
        if backend is not None and space is not None and semantic_status.state in {"ready", "behind"}:
            try:
                vectors = backend.embed((query,), timeout=budget(), cancel=cancel)
                if len(vectors) != 1:
                    raise MemoryFailure("invalid_embedding")
                vector = unit_vector(vectors[0], space.dimensions)
                global_hits, count, limited = self._semantic(access, vector, since=None, until=None, minimum=min_similarity, check=budget)
                recent_hits, recent_count, recent_limited = self._semantic(access, vector, since=since, until=until, minimum=min_similarity, check=budget)
                scanned, scan_limited = count + recent_count, limited or recent_limited
                channels["semantic"] = [hit for hit, _ in global_hits]
                channels["recent_semantic"] = [hit for hit, _ in recent_hits]
                similarities.update((hit.message_id, score) for hit, score in global_hits + recent_hits)
                if scan_limited:
                    degraded.append("semantic_scan_limit")
            except MemoryFailure as failure:
                if failure.code in {"unauthorized", "incompatible_contract"}:
                    raise
                degraded.append(failure.code)
            except (sqlite3.Error, ValueError, TypeError):
                degraded.append("semantic_unavailable")
        evidence: dict[str, SearchHit] = {}
        scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}
        for name, hits in channels.items():
            for rank, hit in enumerate(hits, 1):
                message_id = hit.message_id
                evidence[message_id] = hit
                sources.setdefault(message_id, []).append(name)
                scores[message_id] = scores.get(message_id, 0) + 1 / (60 + rank)
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        recent_ids = [key for key in ordered if any(name.startswith("recent_") for name in sources[key])]
        # Reserve a bounded portion, and give unused seats back to the global pool.
        reserved = recent_ids[:min(2, limit // 3)]
        selected = reserved + [key for key in ordered if key not in reserved]
        result: list[RecallHit] = []
        for key in selected:
            try:
                budget()
            except MemoryFailure as failure:
                degraded.append(failure.code)
                break
            current = self.runtime.read(access, key)
            if current is None:
                continue
            state = self.runtime.state(access, key)
            hit = evidence[key]
            if state.status != "active" or state.version != hit.state_version:
                continue
            result.append(RecallHit(hit, tuple(sources[key]), scores[key], similarities.get(key)))
            if len(result) == limit:
                break
        result.sort(key=lambda item: (-item.score, item.evidence.message_id))
        self.runtime._authorize(access)
        reasons = tuple(dict.fromkeys(degraded))
        evidence_state = "candidates" if result else ("incomplete" if reasons else "no_matching_evidence")
        return RecallPage(tuple(result), lexical.index, semantic_status, reasons, evidence_state, since, expanded, chars,
                          window_limited, scanned, scan_limited)
