"""Publish a verified control-replayed archive as an independent local runtime."""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile

from .archive import ControlCheckpoint, EvidenceArchive, _control_head, _database, _digest, _verify_checkpoint
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _atomic_bytes, _atomic_json, _identifier, _json, _load


def _publication_auth(key: bytes, value: dict[str, object]) -> str:
    return hmac.digest(key, b"evernook-runtime-publication-v1\0" + _json(value).encode(), hashlib.sha256).hex()


def _publication_head(db: sqlite3.Connection, key: bytes) -> str:
    digest = hmac.new(key, b"evernook-runtime-state-v1\0", hashlib.sha256)
    for schema in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"):
        digest.update(_json(tuple(schema)).encode() + b"\0")
        if schema[0] == "table":
            name = str(schema[1]).replace('"', '""')
            encoded = [_json([["bytes", value.hex()] if isinstance(value, bytes)
                              else [type(value).__name__, value] for value in row])
                       for row in db.execute(f'SELECT * FROM "{name}"')]
            for row in sorted(encoded):
                digest.update(row.encode() + b"\0")
    return digest.hexdigest()


def publish_replayed_runtime(runtime: MemoryRuntime, access: Access, source: Path, destination: Path, *,
                             checkpoint: ControlCheckpoint, client_id: str, entry: str) -> tuple[Path, Access]:
    """Publish only while the independently held checkpoint matches the locked source ledger."""
    _identifier(client_id)
    _identifier(entry)
    identity = runtime._authorize(access)
    if not identity.archive_review or "admin" not in identity.scopes:
        raise MemoryFailure("archive_forbidden")
    checkpoint = _verify_checkpoint(runtime, checkpoint)
    source, destination = external(source), external(destination)
    if (source.is_relative_to(destination) or destination.is_relative_to(source)
            or destination.is_relative_to(runtime.root) or runtime.root.is_relative_to(destination)
            or source.is_relative_to(runtime.root)):
        raise MemoryFailure("unsafe_archive_path")
    archive = EvidenceArchive(source)
    expected = archive.status()
    request: dict[str, object] = {"version": 1, "destination": str(destination), "source_sha": expected.sha256,
               "control_head": checkpoint.control_head, "ledger_id": checkpoint.ledger_id,
               "client_id": client_id, "entry": entry, "synchronization": "none",
               "residuals": ["source_ledger", "archives_and_backups", "host_delivered_copies"]}
    if destination.exists():
        try:
            verify_private_directory(destination, extra_files=(destination / "publication.json",))
            receipt = _load(destination / "publication.json")
            authentication = receipt.pop("authentication", None)
            published_head = receipt.get("published_head")
            if (not isinstance(authentication, str)
                    or not hmac.compare_digest(authentication, _publication_auth(runtime._fingerprint_key, receipt))
                    or not isinstance(published_head, str)
                    or receipt != {**request, "client_sha": _digest(destination / "client.json"), "published_head": published_head}
                    or not (destination / "memory.sqlite3").is_file()):
                raise MemoryFailure("archive_destination_exists")
            with runtime._connection() as live:
                live.execute("BEGIN IMMEDIATE")
                if not hmac.compare_digest(_control_head(live, runtime._fingerprint_key), checkpoint.control_head):
                    raise MemoryFailure("archive_checkpoint_stale")
                with closing(sqlite3.connect((destination / "memory.sqlite3").as_uri() + "?mode=ro", uri=True)) as existing:
                    existing.row_factory = sqlite3.Row
                    existing.execute("PRAGMA trusted_schema=OFF")
                    if (existing.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                            or existing.execute("PRAGMA foreign_key_check").fetchone()
                            or not hmac.compare_digest(_publication_head(existing, runtime._fingerprint_key), published_head)):
                        raise MemoryFailure("archive_destination_exists")
            client = _load(destination / "client.json")
            previous_access = Access(client_id, client["token"])
            previous_runtime = MemoryRuntime(destination / "runtime.json")
            if previous_runtime.root != destination:
                raise MemoryFailure("archive_destination_exists")
            previous_runtime._authorize(previous_access)
            return destination / "runtime.json", previous_access
        except MemoryFailure as error:
            if error.code in {"archive_checkpoint_stale", "archive_forbidden", "unauthorized"}:
                raise
            raise MemoryFailure("archive_destination_exists") from None
        except (OSError, sqlite3.Error, KeyError, TypeError):
            raise MemoryFailure("archive_destination_exists") from None
    new_access = Access(client_id, secrets.token_urlsafe(32))
    try:
        with runtime._connection() as live:
            live.execute("BEGIN IMMEDIATE")
            if archive.control_coverage(runtime, access, checkpoint=checkpoint).state != "current_at_check":
                raise MemoryFailure("archive_control_incomplete")
            with tempfile.TemporaryDirectory(prefix=".evernook-runtime-restore-", dir=destination.parent) as temporary:
                staging = Path(temporary) / "runtime"
                provision_private_directory(staging)
                database = staging / "memory.sqlite3"
                shutil.copyfile(source / "evidence.sqlite3", database)
                if _digest(database) != expected.sha256:
                    raise MemoryFailure("archive_integrity_mismatch")
                with _database(database) as archived:
                    current_ids = {str(row[0]) for row in live.execute("SELECT id FROM messages")
                                   if MemoryRuntime._state(live, str(row[0])).status != "purged"}
                    archived_ids = {str(row[0]) for row in archived.execute("SELECT id FROM messages")}
                    if current_ids != archived_ids:
                        raise MemoryFailure("archive_evidence_incomplete")
                    current_important = {tuple(row) for row in live.execute("SELECT * FROM important")
                                         if str(row[1]) in current_ids}
                    archived_important = {tuple(row) for row in archived.execute("SELECT * FROM important")}
                    if current_important != archived_important:
                        raise MemoryFailure("archive_evidence_incomplete")
                    # Do not silently discard newer durable decisions or maintenance state.
                    for table in ("source_context", "text_streams_v1", "text_stream_chunks_v1", "fact_heads_v1", "fact_decisions_v1", "fact_tombstones_v1",
                                  "profile_events_v1", "profile_approvals_v1", "experience_events_v1",
                                  "experience_approvals_v1", "persona_requests_v1", "persona_versions_v1",
                                  "candidate_runs_v1", "fact_candidates_v1", "consolidation_drafts_v1",
                                  "consolidation_results_v1", "consolidation_settings_v1", "preset_settings_v1",
                                  "navigation_policies_v1", "navigation_jobs_v1", "navigation_sources_v1",
                                  "scheduler_settings_v1", "scheduler_progress_v1", "scheduler_sessions_v1", "continuation_checkpoints_v1",
                                  "preference_updates_settings_v1"):
                        def rows(db: sqlite3.Connection) -> set[tuple[object, ...]]:
                            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                            return {tuple(row) for row in db.execute(f"SELECT * FROM {table}")} if exists else set()
                        if rows(live) != rows(archived):
                            raise MemoryFailure("archive_evidence_incomplete")
                # New control evidence may have been copied into the review archive without
                # its maintenance job. Take the source's current job states, never older ones.
                with closing(sqlite3.connect(database)) as restored_db:
                    with restored_db:
                        restored_db.execute("DELETE FROM jobs")
                        restored_db.executemany("INSERT INTO jobs VALUES (?,?)", (
                            tuple(row) for row in live.execute("SELECT * FROM jobs") if str(row[0]) in current_ids))
                _atomic_json(staging / "runtime.json", {"schema_version": 1, "data_dir": str(staging), "busy_timeout_ms": 5000})
                _atomic_json(staging / "client.json", {"schema_version": 2, "client_id": client_id,
                    "entry": entry, "token": new_access.token, "archive_review": False})
                _atomic_bytes(staging / "idempotency.key", runtime._fingerprint_key)
                restored = MemoryRuntime(staging / "runtime.json")
                restored._authorize(new_access)
                if not hmac.compare_digest(_control_head(live, runtime._fingerprint_key), checkpoint.control_head):
                    raise MemoryFailure("archive_checkpoint_stale")
                final_identity = runtime._authorize(access)
                if not final_identity.archive_review or "admin" not in final_identity.scopes:
                    raise MemoryFailure("archive_forbidden")
                _atomic_json(staging / "runtime.json", {"schema_version": 1, "data_dir": str(destination), "busy_timeout_ms": 5000})
                with restored._connection() as restored_db:
                    published_head = _publication_head(restored_db, runtime._fingerprint_key)
                receipt = {**request, "client_sha": _digest(staging / "client.json"), "published_head": published_head}
                _atomic_json(staging / "publication.json", {**receipt,
                    "authentication": _publication_auth(runtime._fingerprint_key, receipt)})
                os.rename(staging, destination)
            live.execute("COMMIT")
    except (OSError, sqlite3.Error):
        raise MemoryFailure("archive_write_failed") from None
    return destination / "runtime.json", new_access
