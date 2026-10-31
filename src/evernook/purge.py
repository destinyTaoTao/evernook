"""Explicit, confirmed destruction of ordinary source evidence and derived assets."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import sqlite3

from .models import Access, Change, MemoryFailure, MessageState
from .runtime import MemoryRuntime, _identifier, _json, _time


_RESIDUALS = ("existing_archives_and_backups_retained", "host_copies_not_controlled")


@dataclass(frozen=True)
class PurgeDecision:
    request_id: str
    confirmation_id: str


@dataclass(frozen=True)
class PurgePreview:
    request_id: str
    source_ids: tuple[str, ...]
    expected_versions: tuple[int, ...]
    created_at: str
    residuals: tuple[str, ...] = _RESIDUALS


@dataclass(frozen=True)
class PurgeResult:
    request_id: str
    state: str
    source_ids: tuple[str, ...]
    residuals: tuple[str, ...] = _RESIDUALS


class PurgeWorkflow:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS purge_requests_v1 (
            request_id TEXT PRIMARY KEY, fingerprint BLOB NOT NULL, preview TEXT NOT NULL,
            state TEXT NOT NULL, confirmation_id TEXT, result TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS purge_targets_v1 (
            request_id TEXT NOT NULL REFERENCES purge_requests_v1(request_id), message_id TEXT NOT NULL,
            source TEXT NOT NULL, fingerprint BLOB NOT NULL, expected_version INTEGER NOT NULL,
            PRIMARY KEY(request_id,message_id))""")

    @staticmethod
    def _preview(raw: str) -> PurgePreview:
        value = json.loads(raw)
        value["source_ids"] = tuple(value["source_ids"])
        value["expected_versions"] = tuple(value["expected_versions"])
        value["residuals"] = tuple(value["residuals"])
        return PurgePreview(**value)

    @staticmethod
    def _result(raw: str) -> PurgeResult:
        value = json.loads(raw)
        value["source_ids"] = tuple(value["source_ids"])
        value["residuals"] = tuple(value["residuals"])
        return PurgeResult(**value)

    @staticmethod
    def _check_scope(db: sqlite3.Connection, source_ids: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in source_ids)
        dependencies = (
            f"SELECT 1 FROM captures WHERE input_id IN ({placeholders}) AND message_id NOT IN ({placeholders}) LIMIT 1",
            f"SELECT 1 FROM changes WHERE evidence_id IN ({placeholders}) AND target_id NOT IN ({placeholders}) LIMIT 1",
            f"SELECT 1 FROM visibility WHERE replacement_id IN ({placeholders}) AND message_id NOT IN ({placeholders}) LIMIT 1",
        )
        if any(db.execute(query, (*source_ids, *source_ids)).fetchone() for query in dependencies):
            raise MemoryFailure("purge_scope_has_dependents")
        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='protected_links_v1'").fetchone()
                and db.execute(f"SELECT 1 FROM protected_links_v1 WHERE message_id IN ({placeholders}) LIMIT 1", source_ids).fetchone()):
            raise MemoryFailure("protected_purge_unsupported")

    def preview(self, access: Access, request_id: str, source_ids: tuple[str, ...]) -> PurgePreview:
        self.runtime._require_scope(access, "admin", "purge_preview")
        _identifier(request_id)
        if (not isinstance(source_ids, tuple) or not 1 <= len(source_ids) <= 16
                or any(not isinstance(item, str) for item in source_ids)):
            raise MemoryFailure("invalid_purge_scope")
        source_ids = tuple(sorted(set(source_ids)))
        for message_id in source_ids:
            _identifier(message_id)
        fingerprint = hmac.digest(self.runtime._fingerprint_key, _json(source_ids).encode(), hashlib.sha256)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            existing = db.execute("SELECT fingerprint,preview FROM purge_requests_v1 WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                    raise MemoryFailure("purge_request_conflict")
                return self._preview(existing["preview"])
            rows = []
            for message_id in source_ids:
                row = db.execute("""SELECT m.id,m.source,m.fingerprint,COALESCE(v.version,1) AS version,
                    json_extract(m.receipt,'$.original_saved') AS original_saved
                    FROM messages m LEFT JOIN visibility v ON v.message_id=m.id WHERE m.id=?""", (message_id,)).fetchone()
                if row is None:
                    raise MemoryFailure("message_not_found")
                if not row["original_saved"]:
                    raise MemoryFailure("protected_purge_unsupported")
                rows.append(row)
            self._check_scope(db, source_ids)
            preview = PurgePreview(request_id, source_ids, tuple(int(row["version"]) for row in rows),
                                   datetime.now(timezone.utc).isoformat())
            db.execute("INSERT INTO purge_requests_v1 VALUES (?,?,?,'previewed',NULL,NULL)",
                       (request_id, fingerprint, _json(asdict(preview))))
            for row in rows:
                db.execute("INSERT INTO purge_targets_v1 VALUES (?,?,?,?,?)",
                           (request_id, row["id"], row["source"], row["fingerprint"], row["version"]))
            db.execute("COMMIT")
            return preview

    def _confirmation(self, db: sqlite3.Connection, preview: PurgePreview, confirmation_id: str) -> None:
        if confirmation_id in preview.source_ids:
            raise MemoryFailure("purge_confirmation_in_scope")
        row = db.execute("SELECT event,receipt,received FROM messages WHERE id=?", (confirmation_id,)).fetchone()
        if row is None:
            raise MemoryFailure("purge_user_confirmation_required")
        message = self.runtime._stored(row)
        if (message.event.role != "user" or message.event.completion != "complete" or not message.event.text.strip()
                or not message.receipt.original_saved or self.runtime._state(db, confirmation_id).status != "active"
                or _time(row["received"]) <= _time(preview.created_at)):
            raise MemoryFailure("purge_user_confirmation_required")

    @staticmethod
    def _clean_derived(db: sqlite3.Connection, ids: tuple[str, ...]) -> None:
        targets = set(ids)
        placeholders = ",".join("?" for _ in ids)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_runs_v1'").fetchone():
            runs = [str(row["id"]) for row in db.execute("SELECT id,sources FROM candidate_runs_v1")
                    if targets.intersection(item[0] for item in json.loads(row["sources"]))]
            for run_id in runs:
                db.execute("DELETE FROM fact_candidates_v1 WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM candidate_runs_v1 WHERE id=?", (run_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_decisions_v1'").fetchone():
            affected: set[tuple[str, str]] = set()
            for row in db.execute("SELECT id,subject,predicate,record FROM fact_decisions_v1").fetchall():
                record = json.loads(row["record"])
                references = {span["message_id"] for span in record["sources"]} | {record["confirmation_id"]}
                if targets.intersection(references):
                    affected.add((str(row["subject"]), str(row["predicate"])))
                    db.execute("DELETE FROM fact_decisions_v1 WHERE id=?", (row["id"],))
            for subject, predicate in affected:
                remaining = db.execute("SELECT COALESCE(max(revision),0) FROM fact_decisions_v1 WHERE subject=? AND predicate=?",
                                       (subject, predicate)).fetchone()[0]
                if remaining:
                    db.execute("UPDATE fact_heads_v1 SET revision=? WHERE subject=? AND predicate=?", (remaining, subject, predicate))
                else:
                    db.execute("DELETE FROM fact_heads_v1 WHERE subject=? AND predicate=?", (subject, predicate))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_events_v1'").fetchone():
            affected_ids: set[str] = set()
            for row in db.execute("SELECT experience_id,record FROM experience_events_v1").fetchall():
                record = json.loads(row["record"])
                references = {span["message_id"] for span in record["draft"]["sources"]} | {record["confirmation_id"]}
                if targets.intersection(references):
                    affected_ids.add(str(row["experience_id"]))
            for experience_id in affected_ids:
                db.execute("DELETE FROM experience_approvals_v1 WHERE experience_id=?", (experience_id,))
                db.execute("DELETE FROM experience_events_v1 WHERE experience_id=?", (experience_id,))
            db.execute(f"DELETE FROM experience_approvals_v1 WHERE message_id IN ({placeholders})", ids)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='consolidation_drafts_v1'").fetchone():
            affected_drafts = [str(row["candidate_id"]) for row in db.execute("SELECT candidate_id,sources FROM consolidation_drafts_v1").fetchall()
                               if targets.intersection(item["message_id"] for item in json.loads(row["sources"]))]
            for candidate_id in affected_drafts:
                db.execute("DELETE FROM consolidation_drafts_v1 WHERE candidate_id=?", (candidate_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
            affected_tags: set[str] = set()
            for row in db.execute("SELECT tag_id,record FROM profile_events_v1").fetchall():
                record = json.loads(row["record"])
                references = {span["message_id"] for span in record["draft"]["sources"]}
                if record["confirmation_id"] is not None:
                    references.add(record["confirmation_id"])
                if targets.intersection(references):
                    affected_tags.add(str(row["tag_id"]))
            for tag_id in affected_tags:
                # Profile tags are derived evidence-backed understanding: like facts and experiences,
                # a tag whose evidence or confirmation is purged is removed instead of left dangling.
                db.execute("DELETE FROM profile_events_v1 WHERE tag_id=?", (tag_id,))
                db.execute("DELETE FROM profile_approvals_v1 WHERE tag_id=?", (tag_id,))
            # Approval bindings are message-backed rows with a real foreign key; leaving them behind
            # would make the messages delete fail with a foreign-key violation.
            db.execute(f"DELETE FROM profile_approvals_v1 WHERE message_id IN ({placeholders})", ids)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='navigation_sources_v1'").fetchone():
            jobs = db.execute(f"SELECT DISTINCT policy,bucket FROM navigation_sources_v1 WHERE message_id IN ({placeholders})", ids).fetchall()
            for job in jobs:
                db.execute("""UPDATE navigation_jobs_v1 SET revision=revision+1,state='queued',lease=NULL,
                    lease_until=NULL,result=NULL,error_code=NULL WHERE policy=? AND bucket=?""", (job["policy"], job["bucket"]))
            db.execute(f"DELETE FROM navigation_sources_v1 WHERE message_id IN ({placeholders})", ids)

    def purge(self, access: Access, decision: PurgeDecision) -> PurgeResult:
        self.runtime._require_scope(access, "admin", "purge")
        if not isinstance(decision, PurgeDecision):
            raise MemoryFailure("invalid_purge_decision")
        _identifier(decision.request_id)
        _identifier(decision.confirmation_id)
        with self.runtime._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM purge_requests_v1 WHERE request_id=?", (decision.request_id,)).fetchone()
            if row is None:
                raise MemoryFailure("purge_preview_required")
            if row["confirmation_id"] is not None and row["confirmation_id"] != decision.confirmation_id:
                raise MemoryFailure("purge_decision_conflict")
            if row["result"] is not None:
                return self._result(row["result"])
            preview = self._preview(row["preview"])
            if row["state"] == "previewed":
                self._confirmation(db, preview, decision.confirmation_id)
                self._check_scope(db, preview.source_ids)
                for target in db.execute("SELECT * FROM purge_targets_v1 WHERE request_id=? ORDER BY message_id", (decision.request_id,)).fetchall():
                    try:
                        current = self.runtime._state(db, target["message_id"])
                    except MemoryFailure as failure:
                        if failure.code != "message_not_found":
                            raise
                        # Messages are only deleted by a completed purge phase 2. Another confirmed
                        # request over the same id therefore already satisfied this target: complete
                        # this request as a no-op instead of reporting a misleading missing message.
                        other = db.execute("SELECT 1 FROM purge_targets_v1 WHERE message_id=? AND request_id<>? LIMIT 1",
                                           (target["message_id"], decision.request_id)).fetchone()
                        if other is None:
                            raise
                        continue
                    if current.version != target["expected_version"]:
                        raise MemoryFailure("purge_scope_changed")
                    change_id = decision.request_id + ":" + target["message_id"]
                    change = Change(change_id, target["message_id"], current.version, "purge", decision.confirmation_id,
                                    "explicit confirmed purge")
                    state = MessageState(target["message_id"], current.version + 1, "purged", None, change_id)
                    db.execute("INSERT INTO changes VALUES (?,?,?,?,?,?,?)",
                               (change_id, target["message_id"], decision.confirmation_id, b"", _json(asdict(change)),
                                _json(asdict(state)), datetime.now(timezone.utc).isoformat()))
                    db.execute("INSERT INTO visibility VALUES (?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET version=excluded.version,status=excluded.status,replacement_id=NULL,change_id=excluded.change_id",
                               (target["message_id"], state.version, state.status, None, state.change_id))
                db.execute("UPDATE purge_requests_v1 SET state='blocked',confirmation_id=? WHERE request_id=?",
                           (decision.confirmation_id, decision.request_id))
            db.execute("COMMIT")
        with self.runtime._connection() as db:
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise MemoryFailure("storage_busy")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            targets = db.execute("SELECT message_id FROM purge_targets_v1 WHERE request_id=?", (decision.request_id,)).fetchall()
            ids = tuple(str(item["message_id"]) for item in targets)
            placeholders = ",".join("?" for _ in ids)
            self._clean_derived(db, ids)
            for table, column in (("important", "message_id"), ("attachments", "message_id"),
                                  ("source_context", "source_id"), ("lexical_docs", "message_id"),
                                  ("semantic_chunks_v1", "message_id"), ("semantic_documents_v1", "message_id"),
                                  ("semantic_failures_v1", "message_id"), ("jobs", "message_id"), ("captures", "message_id")):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    db.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='lexical'").fetchone():
                db.execute(f"DELETE FROM lexical WHERE message_id IN ({placeholders})", ids)
            db.execute(f"DELETE FROM visibility WHERE message_id IN ({placeholders})", ids)
            db.execute(f"DELETE FROM changes WHERE target_id IN ({placeholders})", ids)
            db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
            result = PurgeResult(decision.request_id, "completed", preview.source_ids)
            db.execute("UPDATE purge_requests_v1 SET state='completed',result=? WHERE request_id=?",
                       (_json(asdict(result)), decision.request_id))
            db.execute("COMMIT")
            return result
