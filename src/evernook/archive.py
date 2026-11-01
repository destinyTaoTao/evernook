"""Ordinary evidence archives. Restored archives remain outside normal recall."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from .models import ArchiveView, CaptureReceipt, Change, ChangeRecord, ImportantItem, ImportantPage, MemoryFailure, MessageState
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _identifier, _json, _load, _time

if TYPE_CHECKING:
    from .experience import ExperienceView
    from .facts import FactView
    from .profile import ProfileView
    from .models import Access


@dataclass(frozen=True)
class ArchiveStatus:
    archive_id: str
    sha256: str
    messages: int
    important_items: int
    attachments: int
    recovery_state: str = "review_required"
    backup_enabled: bool = False


@dataclass(frozen=True)
class CaptureReview:
    receipt: CaptureReceipt
    state: MessageState
    archive_only: bool = True


@dataclass(frozen=True)
class ImportantReview:
    page: ImportantPage
    state: MessageState
    archive_only: bool = True


@dataclass(frozen=True)
class ControlCoverage:
    state: str
    control_head: str | None
    recovery_state: str = "review_required"


@dataclass(frozen=True)
class ControlCheckpoint:
    ledger_id: str
    control_head: str
    captured_at: str
    authentication: str


@dataclass(frozen=True)
class FactReview:
    view: FactView
    archive_only: bool = True


@dataclass(frozen=True)
class ProfileReview:
    view: ProfileView
    archive_only: bool = True


@dataclass(frozen=True)
class ExperienceReview:
    view: ExperienceView
    archive_only: bool = True


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _archive_auth(key: bytes, manifest: dict[str, object]) -> str:
    return hmac.digest(key, b"evernook-archive-auth-v2\0" + _json(manifest).encode(), hashlib.sha256).hex()


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8 * 1048576)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 30
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        yield db
    finally:
        db.close()


def _verify(root: Path) -> ArchiveStatus:
    root = external(root)
    if any((root / f"evidence.sqlite3{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise MemoryFailure("archive_not_sealed")
    if {path.name for path in root.iterdir()} != {"manifest.json", "evidence.sqlite3"}:
        raise MemoryFailure("invalid_archive")
    for name in ("manifest.json", "evidence.sqlite3"):
        if external(root / name).parent != root:
            raise MemoryFailure("unsafe_archive_path")
    verify_private_directory(root, extra_files=(root / "manifest.json", root / "evidence.sqlite3"))
    if (root / "manifest.json").stat().st_size > 65536:
        raise MemoryFailure("invalid_archive")
    manifest = _load(root / "manifest.json")
    version = manifest.get("version")
    expected_fields = {"format", "version", "storage_version", "created_at", "status"}
    if version == 2:
        expected_fields.add("source_auth")
    if (set(manifest) != expected_fields
            or manifest["format"] != "evernook-ordinary-archive" or type(manifest["version"]) is not int
            or manifest["version"] not in (1, 2) or type(manifest["storage_version"]) is not int or manifest["storage_version"] != 4):
        raise MemoryFailure("unsupported_archive")
    if version == 2 and (not isinstance(manifest["source_auth"], str) or len(manifest["source_auth"]) != 64
                         or any(char not in "0123456789abcdef" for char in manifest["source_auth"])):
        raise MemoryFailure("invalid_archive")
    value = manifest["status"]
    if (not isinstance(value, dict) or set(value) != set(ArchiveStatus.__dataclass_fields__)
            or any(type(value[key]) is not int or value[key] < 0 for key in ("messages", "important_items", "attachments"))
            or not isinstance(value["archive_id"], str) or len(value["archive_id"]) != 36
            or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64
            or value["recovery_state"] != "review_required" or value["backup_enabled"] is not False):
        raise MemoryFailure("invalid_archive")
    UUID(value["archive_id"])
    if not isinstance(manifest["created_at"], str) or any(char not in "0123456789abcdef" for char in value["sha256"]):
        raise MemoryFailure("invalid_archive")
    _time(manifest["created_at"])
    if _digest(root / "evidence.sqlite3") != value["sha256"]:
        raise MemoryFailure("archive_integrity_mismatch")
    with _database(root / "evidence.sqlite3") as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != 4:
            raise MemoryFailure("unsupported_archive")
        if (db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone()
                or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchone()):
            raise MemoryFailure("archive_integrity_mismatch")
        counts = tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("messages", "important", "attachments"))
        if counts != (value["messages"], value["important_items"], value["attachments"]):
            raise MemoryFailure("archive_integrity_mismatch")
        if db.execute("SELECT 1 FROM attachments WHERE length(content)>1048576 LIMIT 1").fetchone():
            raise MemoryFailure("invalid_archive")
        for row in db.execute("SELECT manifest FROM captures"):
            receipt = MemoryRuntime._capture_receipt(row["manifest"])
            for info in receipt.attachments:
                if type(info.saved) is not bool:
                    raise MemoryFailure("invalid_archive")
                if info.saved:
                    if type(info.size) is not int or not 0 <= info.size <= 1048576:
                        raise MemoryFailure("invalid_archive")
                    MemoryRuntime._attachment_data(db, receipt.message.message_id, info)
    return ArchiveStatus(**value)


def verify_archive(root: Path) -> ArchiveStatus:
    """Verify a local owner-only export; hashes prove integrity, not authenticity or currency."""
    try:
        return _verify(root)
    except MemoryFailure as error:
        if error.code in {"invalid_config", "invalid_time"}:
            raise MemoryFailure("invalid_archive") from None
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        raise MemoryFailure("invalid_archive") from None


def _publish(destination: Path, build: Callable[[Path], None]) -> ArchiveStatus:
    destination = external(destination)
    if destination.exists():
        raise MemoryFailure("archive_destination_exists")
    try:
        with tempfile.TemporaryDirectory(prefix=".evernook-archive-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "archive"
            provision_private_directory(staging)
            build(staging)
            result = verify_archive(staging)
            os.rename(staging, destination)
        return result
    except (OSError, sqlite3.Error):
        raise MemoryFailure("archive_write_failed") from None


def export_archive(runtime: MemoryRuntime, access: Access, destination: Path) -> ArchiveStatus:
    identity = runtime._authorize(access)
    if not identity.archive_review:
        raise MemoryFailure("archive_forbidden")
    if external(destination).is_relative_to(runtime.root):
        raise MemoryFailure("unsafe_archive_path")

    def build(staging: Path) -> None:
        snapshot = staging / "evidence.sqlite3"
        deadline = time.monotonic() + 30

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() > deadline:
                raise MemoryFailure("archive_snapshot_timeout")

        with runtime._connection() as source:
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target, pages=256, progress=progress, sleep=0.05)
            finally:
                target.close()
        if not runtime._authorize(access).archive_review:
            raise MemoryFailure("archive_forbidden")
        with snapshot.open("rb+") as output:
            os.fsync(output.fileno())
        with _database(snapshot) as db:
            counts = [int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in ("messages", "important", "attachments")]
        status = ArchiveStatus(str(uuid4()), _digest(snapshot), counts[0], counts[1], counts[2])
        manifest = {"format": "evernook-ordinary-archive", "version": 2, "storage_version": 4,
                    "created_at": datetime.now(timezone.utc).isoformat(), "status": asdict(status)}
        manifest["source_auth"] = _archive_auth(runtime._fingerprint_key, manifest)
        with (staging / "manifest.json").open("x", encoding="utf-8") as output_manifest:
            output_manifest.write(_json(manifest))
            output_manifest.flush()
            os.fsync(output_manifest.fileno())

    return _publish(destination, build)


def restore_archive(source: Path, destination: Path) -> EvidenceArchive:
    """Publish a verified review copy. Never provisions runtime identities, keys, or recall."""
    expected = verify_archive(source)
    source, destination = external(source), external(destination)
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise MemoryFailure("unsafe_archive_path")
    if destination.exists():
        try:
            matches = verify_archive(destination) == expected
        except MemoryFailure:
            # Any existing non-matching destination — including files that are not archives at all —
            # must report the same occupied-destination code as the export path.
            matches = False
        if not matches:
            raise MemoryFailure("archive_destination_exists")
        return EvidenceArchive(destination)

    def build(staging: Path) -> None:
        for name in ("evidence.sqlite3", "manifest.json"):
            with (source / name).open("rb") as input_file, (staging / name).open("xb") as output:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
        if verify_archive(staging) != expected:
            raise MemoryFailure("archive_integrity_mismatch")

    _publish(destination, build)
    return EvidenceArchive(destination)


def _control_head(db: sqlite3.Connection, key: bytes) -> str:
    digest = hmac.new(key, b"evernook-control-head-v1\0", hashlib.sha256)
    for table in ("ledger_identity_v1", "messages", "captures", "attachments", "changes", "visibility", "purge_requests_v1", "purge_targets_v1",
                  "fact_heads_v1", "fact_decisions_v1", "fact_tombstones_v1", "profile_events_v1",
                  "profile_approvals_v1", "experience_events_v1", "experience_approvals_v1"):
        digest.update(table.encode() + b"\0")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                digest.update(_json([["bytes", value.hex()] if isinstance(value, bytes)
                                     else [type(value).__name__, value] for value in row]).encode())
                digest.update(b"\0")
    return digest.hexdigest()


def capture_control_checkpoint(runtime: MemoryRuntime, access: Access) -> ControlCheckpoint:
    """Capture a key-bound control head to preserve independently as the trusted watermark."""
    identity = runtime._authorize(access)
    if not identity.archive_review or "admin" not in identity.scopes:
        raise MemoryFailure("archive_forbidden")
    with runtime._connection() as db:
        db.execute("BEGIN")
        ledger_id = str(db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()[0])
        head = _control_head(db, runtime._fingerprint_key)
    captured_at = datetime.now(timezone.utc).isoformat()
    payload = _json(["evernook-control-checkpoint-v1", ledger_id, head, captured_at])
    authentication = hmac.digest(runtime._fingerprint_key, payload.encode(), hashlib.sha256).hex()
    final_identity = runtime._authorize(access)
    if not final_identity.archive_review or "admin" not in final_identity.scopes:
        raise MemoryFailure("archive_forbidden")
    return ControlCheckpoint(ledger_id, head, captured_at, authentication)


def _verify_checkpoint(runtime: MemoryRuntime, checkpoint: ControlCheckpoint | None) -> ControlCheckpoint:
    if not isinstance(checkpoint, ControlCheckpoint):
        raise MemoryFailure("archive_checkpoint_required")
    if any(not isinstance(value, str) for value in (checkpoint.ledger_id, checkpoint.control_head,
                                                     checkpoint.captured_at, checkpoint.authentication)):
        raise MemoryFailure("archive_checkpoint_unverified")
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
           for value in (checkpoint.ledger_id, checkpoint.control_head, checkpoint.authentication)):
        raise MemoryFailure("archive_checkpoint_unverified")
    try:
        _time(checkpoint.captured_at)
    except MemoryFailure:
        raise MemoryFailure("archive_checkpoint_unverified") from None
    payload = _json(["evernook-control-checkpoint-v1", checkpoint.ledger_id, checkpoint.control_head, checkpoint.captured_at])
    expected = hmac.digest(runtime._fingerprint_key, payload.encode(), hashlib.sha256).hex()
    if not hmac.compare_digest(checkpoint.authentication, expected):
        raise MemoryFailure("archive_checkpoint_unverified")
    return checkpoint


def replay_archive_controls(runtime: MemoryRuntime, access: Access, source: Path, destination: Path, *,
                            checkpoint: ControlCheckpoint | None = None) -> EvidenceArchive:
    """Replay source-backed changes into an independently published review-only archive."""
    identity = runtime._authorize(access)
    if not identity.archive_review or "admin" not in identity.scopes:
        raise MemoryFailure("archive_forbidden")
    checkpoint = _verify_checkpoint(runtime, checkpoint)
    verify_archive(source)
    source, destination = external(source), external(destination)
    if (source.is_relative_to(destination) or destination.is_relative_to(source)
            or destination.is_relative_to(runtime.root) or source.is_relative_to(runtime.root)):
        raise MemoryFailure("unsafe_archive_path")
    manifest = _load(source / "manifest.json")
    supplied = manifest.pop("source_auth", None)
    if manifest.get("version") != 2 or not isinstance(supplied, str):
        raise MemoryFailure("archive_source_unverified")
    expected = _archive_auth(runtime._fingerprint_key, manifest)
    if not hmac.compare_digest(supplied, expected):
        raise MemoryFailure("archive_source_unverified")
    with runtime._connection() as live:
        live.execute("BEGIN")
        if not hmac.compare_digest(_control_head(live, runtime._fingerprint_key), checkpoint.control_head):
            raise MemoryFailure("archive_checkpoint_stale")
    if destination.exists():
        try:
            verify_archive(destination)
            previous_manifest = _load(destination / "manifest.json")
            previous_auth = previous_manifest.pop("source_auth", None)
            expected_auth = _archive_auth(runtime._fingerprint_key, previous_manifest)
            if not isinstance(previous_auth, str) or not hmac.compare_digest(previous_auth, expected_auth):
                raise MemoryFailure("archive_destination_exists")
            with _database(destination / "evidence.sqlite3") as previous, runtime._connection() as live:
                live.execute("BEGIN")
                if not hmac.compare_digest(_control_head(live, runtime._fingerprint_key), checkpoint.control_head):
                    raise MemoryFailure("archive_checkpoint_stale")
                receipt = previous.execute("SELECT source_sha,control_head FROM control_replay_v1 WHERE id=1").fetchone()
                if (receipt is not None and receipt["source_sha"] == manifest["status"]["sha256"]
                        and hmac.compare_digest(receipt["control_head"], _control_head(live, runtime._fingerprint_key))):
                    return EvidenceArchive(destination)
        except MemoryFailure as error:
            if error.code in {"archive_checkpoint_stale", "archive_forbidden", "unauthorized"}:
                raise
        except (sqlite3.Error, KeyError, TypeError):
            pass
        raise MemoryFailure("archive_destination_exists")

    def build(staging: Path) -> None:
        shutil.copyfile(source / "evidence.sqlite3", staging / "evidence.sqlite3")
        if _digest(staging / "evidence.sqlite3") != manifest["status"]["sha256"]:
            raise MemoryFailure("archive_integrity_mismatch")
        with runtime._connection() as live:
            live.execute("BEGIN")
            ledger = live.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()
            if ledger is None or not hmac.compare_digest(str(ledger[0]), checkpoint.ledger_id):
                raise MemoryFailure("archive_checkpoint_unverified")
            if not hmac.compare_digest(_control_head(live, runtime._fingerprint_key), checkpoint.control_head):
                raise MemoryFailure("archive_checkpoint_stale")
            with closing(sqlite3.connect(staging / "evidence.sqlite3", isolation_level=None)) as copy:
                copy.row_factory = sqlite3.Row
                copy.execute("PRAGMA foreign_keys=ON")
                copy.execute("PRAGMA trusted_schema=OFF")
                copy.execute("PRAGMA secure_delete=ON")
                copy.execute("BEGIN IMMEDIATE")
                archived_ledger = copy.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()
                if archived_ledger is None or not hmac.compare_digest(str(archived_ledger[0]), checkpoint.ledger_id):
                    raise MemoryFailure("archive_control_incomplete")
                old_ids = {str(row[0]) for row in copy.execute("SELECT id FROM messages")}
                for old_message in copy.execute("SELECT * FROM messages"):
                    current = live.execute("SELECT * FROM messages WHERE id=?", (old_message["id"],)).fetchone()
                    if current is not None and tuple(current) != tuple(old_message):
                        raise MemoryFailure("archive_control_incomplete")
                    if current is None or MemoryRuntime._state(live, str(old_message["id"])).status == "purged":
                        continue
                    for table in ("captures", "attachments"):
                        archived_rows = sorted(tuple(row) for row in copy.execute(
                            f"SELECT * FROM {table} WHERE message_id=?", (old_message["id"],)))
                        current_rows = sorted(tuple(row) for row in live.execute(
                            f"SELECT * FROM {table} WHERE message_id=?", (old_message["id"],)))
                        if archived_rows != current_rows:
                            raise MemoryFailure("archive_control_incomplete")

                visiting: set[str] = set()
                def copy_evidence(message_id: str) -> None:
                    if copy.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
                        return
                    if message_id in visiting:
                        raise MemoryFailure("archive_control_incomplete")
                    visiting.add(message_id)
                    message = live.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
                    if message is None:
                        raise MemoryFailure("archive_control_incomplete")
                    state = MemoryRuntime._state(live, message_id)
                    if state.status != "active" or state.version != 1:
                        raise MemoryFailure("archive_control_incomplete")
                    capture = live.execute("SELECT * FROM captures WHERE message_id=?", (message_id,)).fetchone()
                    if capture is not None and capture["input_id"] is not None:
                        copy_evidence(str(capture["input_id"]))
                    copy.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", tuple(message))
                    if capture is not None:
                        copy.execute("INSERT INTO captures VALUES (?,?,?)", tuple(capture))
                        for attachment in live.execute("SELECT * FROM attachments WHERE message_id=?", (message_id,)):
                            copy.execute("INSERT INTO attachments VALUES (?,?,?,?)", tuple(attachment))
                    visiting.remove(message_id)
                from .purge import PurgeWorkflow
                old_purges = copy.execute("SELECT 1 FROM sqlite_master WHERE name='purge_targets_v1'").fetchone()
                live_purges = live.execute("SELECT 1 FROM sqlite_master WHERE name='purge_targets_v1'").fetchone()
                if old_purges:
                    for target in copy.execute("""SELECT t.* FROM purge_targets_v1 t JOIN purge_requests_v1 r
                            ON r.request_id=t.request_id WHERE r.state IN ('blocked','completed')""").fetchall():
                        current = live.execute("""SELECT t.* FROM purge_targets_v1 t JOIN purge_requests_v1 r
                            ON r.request_id=t.request_id WHERE t.request_id=? AND t.message_id=?
                            AND r.state IN ('blocked','completed')""", (target["request_id"], target["message_id"])).fetchone() if live_purges else None
                        if current is None or tuple(current) != tuple(target):
                            raise MemoryFailure("archive_control_incomplete")
                if live_purges:
                    PurgeWorkflow._schema(copy)
                    for request in live.execute("SELECT * FROM purge_requests_v1 WHERE state IN ('blocked','completed')"):
                        copy.execute("""INSERT INTO purge_requests_v1 VALUES (?,?,?,?,?,?)
                            ON CONFLICT(request_id) DO UPDATE SET fingerprint=excluded.fingerprint,preview=excluded.preview,
                            state=excluded.state,confirmation_id=excluded.confirmation_id,result=excluded.result""", tuple(request))
                        for target in live.execute("SELECT * FROM purge_targets_v1 WHERE request_id=?", (request["request_id"],)):
                            copy.execute("INSERT OR IGNORE INTO purge_targets_v1 VALUES (?,?,?,?,?)", tuple(target))
                fact_keys: set[tuple[str, str]] = set()
                profile_ids: set[str] = set()
                if copy.execute("SELECT 1 FROM sqlite_master WHERE name='profile_events_v1'").fetchone():
                    profile_ids.update(str(row[0]) for row in copy.execute("SELECT DISTINCT tag_id FROM profile_events_v1"))
                experience_ids: set[str] = set()
                if copy.execute("SELECT 1 FROM sqlite_master WHERE name='experience_events_v1'").fetchone():
                    experience_ids.update(str(row[0]) for row in copy.execute("SELECT DISTINCT experience_id FROM experience_events_v1"))
                for table in ("fact_decisions_v1", "fact_tombstones_v1"):
                    if copy.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                        fact_keys.update((str(row[0]), str(row[1])) for row in copy.execute(f"SELECT DISTINCT subject,predicate FROM {table}"))
                missing = tuple(message_id for message_id in old_ids
                                if live.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone() is None
                                or MemoryRuntime._state(live, message_id).status == "purged")
                if missing:
                    if len(missing) > 10000 or not live.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='purge_targets_v1'").fetchone():
                        raise MemoryFailure("archive_control_incomplete")
                    for message_id in missing:
                        tombstone = live.execute("""SELECT t.source,t.fingerprint FROM purge_targets_v1 t
                            JOIN purge_requests_v1 r ON r.request_id=t.request_id
                            WHERE t.message_id=? AND r.state IN ('blocked','completed') LIMIT 1""", (message_id,)).fetchone()
                        original = copy.execute("SELECT source,fingerprint FROM messages WHERE id=?", (message_id,)).fetchone()
                        if (tombstone is None or original is None or tombstone["source"] != original["source"]
                                or not hmac.compare_digest(bytes(tombstone["fingerprint"]), bytes(original["fingerprint"]))):
                            raise MemoryFailure("archive_control_incomplete")
                    PurgeWorkflow._check_scope(copy, missing)
                    PurgeWorkflow._clean_derived(copy, missing)
                    placeholders = ",".join("?" for _ in missing)
                    for table, column in (("important", "message_id"), ("attachments", "message_id"),
                                          ("source_context", "source_id"), ("lexical_docs", "message_id"),
                                          ("semantic_chunks_v1", "message_id"), ("semantic_documents_v1", "message_id"),
                                          ("semantic_failures_v1", "message_id"), ("jobs", "message_id"), ("captures", "message_id"),
                                          ("lexical", "message_id")):
                        if copy.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                            copy.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", missing)
                    copy.execute(f"DELETE FROM visibility WHERE message_id IN ({placeholders})", missing)
                    copy.execute(f"DELETE FROM changes WHERE target_id IN ({placeholders})", missing)
                    copy.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", missing)
                    old_ids.difference_update(missing)
                if fact_keys:
                    from .facts import FactRegistry
                    FactRegistry._schema(copy)
                    for subject, predicate in fact_keys:
                        current_facts: list[sqlite3.Row] = []
                        current_tombstones: list[sqlite3.Row] = []
                        if live.execute("SELECT 1 FROM sqlite_master WHERE name='fact_decisions_v1'").fetchone():
                            current_facts = live.execute("SELECT * FROM fact_decisions_v1 WHERE subject=? AND predicate=?",
                                                         (subject, predicate)).fetchall()
                        if live.execute("SELECT 1 FROM sqlite_master WHERE name='fact_tombstones_v1'").fetchone():
                            current_tombstones = live.execute("SELECT * FROM fact_tombstones_v1 WHERE subject=? AND predicate=?",
                                                              (subject, predicate)).fetchall()
                        if not current_facts and not current_tombstones:
                            raise MemoryFailure("archive_control_incomplete")
                        old_head = copy.execute("SELECT revision FROM fact_heads_v1 WHERE subject=? AND predicate=?",
                                                (subject, predicate)).fetchone()
                        head = live.execute("SELECT revision FROM fact_heads_v1 WHERE subject=? AND predicate=?", (subject, predicate)).fetchone()
                        if old_head is None or head is None or int(head[0]) < int(old_head[0]):
                            raise MemoryFailure("archive_control_incomplete")
                        revisions = {int(row["revision"]): tuple(row) for row in (*current_facts, *current_tombstones)}
                        for table in ("fact_decisions_v1", "fact_tombstones_v1"):
                            for prior in copy.execute(f"SELECT * FROM {table} WHERE subject=? AND predicate=?", (subject, predicate)):
                                if revisions.get(int(prior["revision"])) != tuple(prior):
                                    raise MemoryFailure("archive_control_incomplete")
                        for row in current_facts:
                            record = json.loads(row["record"])
                            evidence_ids = {span["message_id"] for span in record["sources"]} | {record["confirmation_id"]}
                            for evidence_id in evidence_ids:
                                copy_evidence(evidence_id)
                        copy.execute("DELETE FROM fact_decisions_v1 WHERE subject=? AND predicate=?", (subject, predicate))
                        copy.execute("DELETE FROM fact_tombstones_v1 WHERE subject=? AND predicate=?", (subject, predicate))
                        for row in current_facts:
                            copy.execute("INSERT INTO fact_decisions_v1 VALUES (?,?,?,?,?,?,?)", tuple(row))
                        for row in current_tombstones:
                            copy.execute("INSERT INTO fact_tombstones_v1 VALUES (?,?,?,?,?)", tuple(row))
                        copy.execute("""INSERT INTO fact_heads_v1 VALUES (?,?,?) ON CONFLICT(subject,predicate)
                            DO UPDATE SET revision=excluded.revision""", (subject, predicate, head[0]))
                def replay_review_events(prefix: str, entity: str, identifiers: set[str]) -> None:
                    events_table, approvals_table = prefix + "_events_v1", prefix + "_approvals_v1"
                    has_events = live.execute("SELECT 1 FROM sqlite_master WHERE name=?", (events_table,)).fetchone()
                    for identifier in identifiers:
                        events = live.execute(f"SELECT * FROM {events_table} WHERE {entity}=? ORDER BY revision", (identifier,)).fetchall() if has_events else []
                        approvals = live.execute(f"SELECT * FROM {approvals_table} WHERE {entity}=?", (identifier,)).fetchall() if has_events else []
                        previous = copy.execute(f"SELECT MAX(revision) FROM {events_table} WHERE {entity}=?", (identifier,)).fetchone()[0]
                        if previous is not None and (not events or int(events[-1]["revision"]) < int(previous)):
                            raise MemoryFailure("archive_control_incomplete")
                        current_by_revision = {int(row["revision"]): tuple(row) for row in events}
                        for prior in copy.execute(f"SELECT * FROM {events_table} WHERE {entity}=?", (identifier,)):
                            if current_by_revision.get(int(prior["revision"])) != tuple(prior):
                                raise MemoryFailure("archive_control_incomplete")
                        references = {str(row["message_id"]) for row in approvals}
                        for row in events:
                            record = json.loads(row["record"])
                            references.update(span["message_id"] for span in record["draft"]["sources"])
                            if record["confirmation_id"] is not None:
                                references.add(record["confirmation_id"])
                        for evidence_id in references:
                            copy_evidence(evidence_id)
                        copy.execute(f"DELETE FROM {approvals_table} WHERE {entity}=?", (identifier,))
                        copy.execute(f"DELETE FROM {events_table} WHERE {entity}=?", (identifier,))
                        for row in events:
                            copy.execute(f"INSERT INTO {events_table} VALUES (?,?,?,?,?)", tuple(row))
                        for row in approvals:
                            copy.execute(f"INSERT INTO {approvals_table} VALUES (?,?,?,?)", tuple(row))
                if profile_ids:
                    from .profile import ProfileRegistry
                    ProfileRegistry._schema(copy)
                    replay_review_events("profile", "tag_id", profile_ids)
                if experience_ids:
                    from .experience import ExperienceRegistry
                    ExperienceRegistry._schema(copy)
                    replay_review_events("experience", "experience_id", experience_ids)
                changes = live.execute("SELECT * FROM changes ORDER BY recorded_at,request_id").fetchall()
                for prior in copy.execute("SELECT * FROM changes"):
                    if str(prior["target_id"]) in old_ids:
                        current = live.execute("SELECT * FROM changes WHERE request_id=?", (prior["request_id"],)).fetchone()
                        if current is None or tuple(current) != tuple(prior):
                            raise MemoryFailure("archive_control_incomplete")
                # Include the authentic confirmation evidence needed by each applicable control.
                applicable = [row for row in changes if str(row["target_id"]) in old_ids]
                for row in applicable:
                    copy_evidence(str(row["evidence_id"]))
                    copy.execute("INSERT OR IGNORE INTO changes VALUES (?,?,?,?,?,?,?)", tuple(row))
                for message_id in old_ids:
                    state = live.execute("SELECT * FROM visibility WHERE message_id=?", (message_id,)).fetchone()
                    previous = copy.execute("SELECT version FROM visibility WHERE message_id=?", (message_id,)).fetchone()
                    if previous is not None and (state is None or int(state["version"]) < int(previous["version"])):
                        raise MemoryFailure("archive_control_incomplete")
                    if state is not None:
                        if str(state["change_id"]) not in {str(row["request_id"]) for row in applicable}:
                            raise MemoryFailure("archive_control_incomplete")
                        copy.execute("INSERT OR REPLACE INTO visibility VALUES (?,?,?,?,?)", tuple(state))
                copy.execute("""CREATE TABLE IF NOT EXISTS control_replay_v1 (id INTEGER PRIMARY KEY CHECK(id=1),
                    source_sha TEXT NOT NULL, control_head TEXT NOT NULL, coverage TEXT NOT NULL)""")
                copy.execute("INSERT OR REPLACE INTO control_replay_v1 VALUES (1,?,?,?)",
                             (manifest["status"]["sha256"], _control_head(live, runtime._fingerprint_key), "trusted_snapshot"))
                copy.execute("COMMIT")
            live.execute("COMMIT")
        with _database(staging / "evidence.sqlite3") as db:
            counts = [int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in ("messages", "important", "attachments")]
        status = ArchiveStatus(str(uuid4()), _digest(staging / "evidence.sqlite3"), counts[0], counts[1], counts[2])
        result_manifest = {"format": "evernook-ordinary-archive", "version": 2, "storage_version": 4,
                           "created_at": datetime.now(timezone.utc).isoformat(), "status": asdict(status)}
        result_manifest["source_auth"] = _archive_auth(runtime._fingerprint_key, result_manifest)
        with (staging / "manifest.json").open("x", encoding="utf-8") as output:
            output.write(_json(result_manifest))
            output.flush()
            os.fsync(output.fileno())
        final_identity = runtime._authorize(access)
        if not final_identity.archive_review or "admin" not in final_identity.scopes:
            raise MemoryFailure("archive_forbidden")

    _publish(destination, build)
    return EvidenceArchive(destination)


class EvidenceArchive:
    """Explicit archive-review boundary; no normal read/search or write operations."""

    def __init__(self, root: Path):
        self.root = external(root)
        verify_archive(self.root)

    def status(self) -> ArchiveStatus:
        return verify_archive(self.root)

    def control_coverage(self, runtime: MemoryRuntime | None = None, access: Access | None = None, *,
                         checkpoint: ControlCheckpoint | None = None) -> ControlCoverage:
        """Compare replay coverage with a trusted live ledger, without enabling ordinary recall."""
        with self._connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='control_replay_v1'").fetchone():
                return ControlCoverage("unverified", None)
            receipt = db.execute("SELECT control_head FROM control_replay_v1 WHERE id=1").fetchone()
            if receipt is None:
                return ControlCoverage("unverified", None)
            head = str(receipt["control_head"])
        if runtime is None or access is None or checkpoint is None:
            return ControlCoverage("unverified", head)
        identity = runtime._authorize(access)
        if not identity.archive_review or "admin" not in identity.scopes:
            raise MemoryFailure("archive_forbidden")
        checkpoint = _verify_checkpoint(runtime, checkpoint)
        manifest = _load(self.root / "manifest.json")
        supplied = manifest.pop("source_auth", None)
        expected = _archive_auth(runtime._fingerprint_key, manifest)
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            return ControlCoverage("unverified", head)
        with runtime._connection() as db:
            db.execute("BEGIN")
            ledger = db.execute("SELECT ledger_id FROM ledger_identity_v1 WHERE id=1").fetchone()
            if ledger is None or not hmac.compare_digest(str(ledger[0]), checkpoint.ledger_id):
                return ControlCoverage("unverified", head)
            current = _control_head(db, runtime._fingerprint_key)
        return ControlCoverage("current_at_check" if hmac.compare_digest(head, current)
                               and hmac.compare_digest(head, checkpoint.control_head) else "stale", head)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        verify_archive(self.root)
        try:
            with _database(self.root / "evidence.sqlite3") as db:
                yield db
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise MemoryFailure("invalid_archive") from None

    def review_message(self, message_id: str) -> ArchiveView:
        _identifier(message_id)
        with self._connection() as db:
            state = MemoryRuntime._state(db, message_id)
            row = db.execute("SELECT event,receipt FROM messages WHERE id=?", (message_id,)).fetchone()
            changes = tuple(ChangeRecord(Change(**json.loads(item["request"])), MessageState(**json.loads(item["receipt"])), item["recorded_at"])
                for item in db.execute("SELECT request,receipt,recorded_at FROM changes WHERE target_id=? ORDER BY json_extract(receipt,'$.version')", (message_id,)))
            return ArchiveView(MemoryRuntime._stored(row), state, changes)

    def review_fact(self, subject_id: str, predicate: str, *, valid_at: str | None = None,
                    known_at: str | None = None) -> FactReview:
        """Review facts in this archive only; this does not grant normal recall."""
        from .facts import FactRegistry
        with self._connection() as db:
            return FactReview(FactRegistry._query_snapshot(db, subject_id, predicate, valid_at=valid_at, known_at=known_at))

    def review_profile(self, tag_id: str) -> ProfileReview:
        from .profile import ProfileRegistry
        _identifier(tag_id)
        with self._connection() as db:
            return ProfileReview(ProfileRegistry._view(db, tag_id))

    def review_experience(self, experience_id: str) -> ExperienceReview:
        from .experience import ExperienceRegistry
        _identifier(experience_id)
        with self._connection() as db:
            return ExperienceReview(ExperienceRegistry._view(db, experience_id))

    def review_message_ids(self, *, offset: int = 0, limit: int = 100) -> tuple[str, ...]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise MemoryFailure("invalid_page")
        with self._connection() as db:
            return tuple(row[0] for row in db.execute("SELECT id FROM messages ORDER BY received,id LIMIT ? OFFSET ?", (limit, offset)))

    def review_capture(self, message_id: str) -> CaptureReview | None:
        _identifier(message_id)
        with self._connection() as db:
            row = db.execute("SELECT manifest FROM captures WHERE message_id=?", (message_id,)).fetchone()
            return CaptureReview(MemoryRuntime._capture_receipt(row["manifest"]), MemoryRuntime._state(db, message_id)) if row else None

    def review_attachment(self, message_id: str, attachment_id: str) -> bytes | None:
        _identifier(message_id)
        _identifier(attachment_id)
        with self._connection() as db:
            row = db.execute("SELECT manifest FROM captures WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                return None
            receipt = MemoryRuntime._capture_receipt(row["manifest"])
            info = next((item for item in receipt.attachments if item.attachment_id == attachment_id), None)
            return MemoryRuntime._attachment_data(db, message_id, info) if info is not None and info.saved else None

    def review_important(self, important_id: str, *, cursor: int | None = None, limit: int = 2048) -> ImportantReview:
        _identifier(important_id)
        if type(limit) is not int or not 1 <= limit <= 8192:
            raise MemoryFailure("invalid_limit")
        with self._connection() as db:
            row = db.execute("SELECT item FROM important WHERE id=?", (important_id,)).fetchone()
            if row is None:
                raise MemoryFailure("important_not_found")
            item = ImportantItem(**json.loads(row["item"]))
            start = item.start if cursor is None else cursor
            if type(start) is not int or not item.start <= start < item.end:
                raise MemoryFailure("invalid_cursor")
            message = db.execute("SELECT event,receipt FROM messages WHERE id=?", (item.message_id,)).fetchone()
            text = MemoryRuntime._stored(message).event.text
            end = min(start + limit, item.end)
            return ImportantReview(ImportantPage(item, text[start:end], start, end, item.end - end, end if end < item.end else None),
                                   MemoryRuntime._state(db, item.message_id))
