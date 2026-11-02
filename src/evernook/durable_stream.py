"""Bounded text chunks committed before publication; no uncommitted raw tail at rest."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import hmac
import json
import sqlite3
from typing import TYPE_CHECKING

from .models import Access, CaptureReceipt, Event, MemoryFailure, Source
from .safety import credential_signal, stream_safe_prefix

if TYPE_CHECKING:
    from .runtime import MemoryRuntime

MAX_CHUNKS = 4096


@dataclass(frozen=True)
class StreamReceipt:
    stream_id: str
    index: int
    state: str
    released_chars: int
    committed_chars: int
    pending_chars: int
    final_message_id: str | None
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class StreamPage:
    stream_id: str
    source: Source
    state: str
    text: str = field(repr=False)
    start: int
    end: int
    next_cursor: int | None
    committed_chars: int
    pending_chars: int
    next_index: int
    complete: bool
    final_message_id: str | None


def install(db: sqlite3.Connection) -> None:
    db.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS text_streams_v1 (
          message_id TEXT PRIMARY KEY REFERENCES messages(id), source TEXT UNIQUE NOT NULL,
          template TEXT NOT NULL, input_id TEXT NOT NULL REFERENCES messages(id), state TEXT NOT NULL,
          next_index INTEGER NOT NULL, committed_chars INTEGER NOT NULL, pending_chars INTEGER NOT NULL,
          carry_hash BLOB NOT NULL, final_message_id TEXT REFERENCES messages(id));
        CREATE TABLE IF NOT EXISTS text_stream_chunks_v1 (
          message_id TEXT NOT NULL REFERENCES text_streams_v1(message_id), chunk_index INTEGER NOT NULL,
          fingerprint BLOB NOT NULL, text TEXT NOT NULL, receipt TEXT NOT NULL,
          PRIMARY KEY(message_id,chunk_index));
        COMMIT;""")


def remove_sources(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
    if not ids or not db.execute("SELECT 1 FROM sqlite_master WHERE name='text_streams_v1'").fetchone():
        return
    slots = ",".join("?" for _ in ids)
    targets = tuple(str(row[0]) for row in db.execute(
        f"SELECT message_id FROM text_streams_v1 WHERE message_id IN ({slots}) OR final_message_id IN ({slots})", ids + ids))
    if targets:
        slots = ",".join("?" for _ in targets)
        db.execute(f"DELETE FROM text_stream_chunks_v1 WHERE message_id IN ({slots})", targets)
        db.execute(f"DELETE FROM text_streams_v1 WHERE message_id IN ({slots})", targets)


def append(runtime: MemoryRuntime, access: Access, event: Event, *, index: int, carry: str, input_id: str) -> StreamReceipt:
    from .runtime import _identifier, _json, _stream_marker_source
    runtime._validate_event(event)
    identity = runtime._require_scope(access, "capture", "append_stream", _json(asdict(event.source)))
    _identifier(input_id)
    if event.source.entry != identity.entry:
        raise MemoryFailure("unauthorized_source")
    if (event.role != "assistant" or type(index) is not int or not 0 <= index <= MAX_CHUNKS
            or (index == MAX_CHUNKS and (event.text or event.completion == "partial"))
            or not isinstance(carry, str) or len(event.text.encode("utf-8")) > 65536):
        raise MemoryFailure("invalid_stream_chunk")
    try:
        carry_bytes = carry.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MemoryFailure("invalid_encoding") from None
    if len(carry_bytes) > 1048576:
        raise MemoryFailure("stream_too_large")
    source = _json(asdict(event.source))
    template = _json(asdict(replace(event, text="", completion="partial", sensitive=False, protected_source=False)))
    def digest(text: str) -> bytes:
        return hmac.digest(runtime._fingerprint_key, text.encode("utf-8"), hashlib.sha256)
    fingerprint = digest(_json({"event": asdict(event), "carry": carry, "input_id": input_id, "index": index}))
    with runtime._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        head = db.execute("SELECT * FROM text_streams_v1 WHERE source=?", (source,)).fetchone()
        if head is None:
            if index != 0:
                raise MemoryFailure("stream_order_conflict")
            if db.execute("SELECT 1 FROM messages WHERE source=?", (source,)).fetchone():
                raise MemoryFailure("source_conflict")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='purge_targets_v1'").fetchone():
                if db.execute("""SELECT 1 FROM purge_targets_v1 t JOIN purge_requests_v1 r ON r.request_id=t.request_id
                    WHERE t.source=? AND r.state IN ('blocked','completed')""", (source,)).fetchone():
                    raise MemoryFailure("source_purged")
            stored = db.execute("SELECT event,receipt FROM messages WHERE id=?", (input_id,)).fetchone()
            if (stored is None or runtime._state(db, input_id).status != "active"
                    or json.loads(stored["event"])["role"] != "user"
                    or json.loads(stored["event"])["completion"] != "complete"
                    or not json.loads(stored["receipt"])["original_saved"]):
                raise MemoryFailure("authentic_input_unavailable")
            marker_source = _stream_marker_source(event.source)
            if db.execute("SELECT 1 FROM messages WHERE source=?", (_json(asdict(marker_source)),)).fetchone():
                raise MemoryFailure("stream_unavailable")
            marker = replace(event, source=marker_source, role="system", completion="partial", text="", sensitive=False, protected_source=False)
            receipt = runtime._persist_event(db, marker, marker, digest(template + input_id))
            manifest = CaptureReceipt(receipt, "message", input_id, None, (), (), True)
            db.execute("INSERT INTO captures VALUES (?,?,?)", (receipt.message_id, input_id, _json(asdict(manifest))))
            db.execute("INSERT INTO text_streams_v1 VALUES (?,?,?,?,?,0,0,0,?,NULL)",
                (receipt.message_id, source, template, input_id, "open", digest("")))
            head = db.execute("SELECT * FROM text_streams_v1 WHERE source=?", (source,)).fetchone()
        assert head is not None
        if head["template"] != template or head["input_id"] != input_id:
            raise MemoryFailure("stream_identity_conflict")
        if runtime._state(db, head["message_id"]).status != "active":
            raise MemoryFailure("stream_unavailable")
        if runtime._state(db, input_id).status != "active":
            raise MemoryFailure("stream_unavailable")
        if head["final_message_id"] and runtime._state(db, head["final_message_id"]).status != "active":
            raise MemoryFailure("stream_unavailable")
        old = db.execute("SELECT fingerprint,receipt FROM text_stream_chunks_v1 WHERE message_id=? AND chunk_index=?",
            (head["message_id"], index)).fetchone()
        if old is not None:
            if not hmac.compare_digest(bytes(old["fingerprint"]), fingerprint):
                raise MemoryFailure("stream_chunk_conflict")
            value = json.loads(old["receipt"])
            value["gaps"] = tuple(value["gaps"])
            return StreamReceipt(**value)
        if head["state"] != "open":
            raise MemoryFailure("stream_closed")
        if index != head["next_index"]:
            raise MemoryFailure("stream_order_conflict")
        stopping = event.completion in {"canceled", "failed"}
        abandoning = stopping and not carry and not event.text
        if not abandoning and not hmac.compare_digest(bytes(head["carry_hash"]), digest(carry)):
            raise MemoryFailure("stream_carry_mismatch")
        combined = carry + event.text
        previous = "".join(str(row[0]) for row in db.execute(
            "SELECT text FROM text_stream_chunks_v1 WHERE message_id=? ORDER BY chunk_index", (head["message_id"],)))
        if len((previous + combined).encode("utf-8")) > 1048576:
            raise MemoryFailure("stream_too_large")
        protected = event.sensitive or event.protected_source or credential_signal(previous + combined)
        state = "protected" if protected else "open" if event.completion == "partial" else event.completion
        count = 0 if protected or stopping else stream_safe_prefix(combined, complete=state == "complete")
        text = combined[:count]
        pending = head["pending_chars"] if abandoning else len(combined) - count
        final_id = None
        if state == "complete":
            full = replace(event, text=previous + text)
            final = runtime._persist_event(db, full, full, digest(_json(asdict(full))), stream_id=head["message_id"])
            manifest = CaptureReceipt(final, "message", head["message_id"], None, (), (), True)
            db.execute("INSERT INTO captures VALUES (?,?,?)", (final.message_id, head["message_id"], _json(asdict(manifest))))
            final_id = final.message_id
        result = StreamReceipt(head["message_id"], index, state, count, len(previous) + count, pending, final_id,
            ("protected_original_not_saved",) if protected else ("unpublished_tail",) if pending else ())
        db.execute("INSERT INTO text_stream_chunks_v1 VALUES (?,?,?,?,?)",
            (head["message_id"], index, fingerprint, text, _json(asdict(result))))
        db.execute("UPDATE text_streams_v1 SET state=?,next_index=?,committed_chars=?,pending_chars=?,carry_hash=?,final_message_id=? WHERE message_id=?",
            (state, index + 1, result.committed_chars, pending, digest(combined[count:]), final_id, head["message_id"]))
        db.execute("COMMIT")
        return result


def read(runtime: MemoryRuntime, access: Access, source: Source, *, cursor: int = 0, limit: int = 2048) -> StreamPage:
    from .runtime import _identifier, _json
    runtime._require_scope(access, "recall", "read_stream")
    if not isinstance(source, Source):
        raise MemoryFailure("invalid_source")
    for value in asdict(source).values():
        _identifier(value)
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 4096:
        raise MemoryFailure("invalid_stream_page")
    with runtime._connection() as db:
        db.execute("BEGIN")
        head = db.execute("SELECT * FROM text_streams_v1 WHERE source=?", (_json(asdict(source)),)).fetchone()
        if head is None or runtime._state(db, head["message_id"]).status != "active":
            raise MemoryFailure("stream_unavailable")
        if runtime._state(db, head["input_id"]).status != "active":
            raise MemoryFailure("stream_unavailable")
        if head["final_message_id"] and runtime._state(db, head["final_message_id"]).status != "active":
            raise MemoryFailure("stream_unavailable")
        text = "".join(str(row[0]) for row in db.execute(
            "SELECT text FROM text_stream_chunks_v1 WHERE message_id=? ORDER BY chunk_index", (head["message_id"],)))
        if cursor > len(text):
            raise MemoryFailure("invalid_stream_page")
        end = min(len(text), cursor + limit)
        return StreamPage(head["message_id"], source, head["state"], text[cursor:end], cursor, end,
            end if end < len(text) else None, len(text), head["pending_chars"], head["next_index"],
            head["state"] == "complete", head["final_message_id"])
