"""Ordinary evidence archives. Restored archives remain outside normal recall."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
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


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


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
    if (set(manifest) != {"format", "version", "storage_version", "created_at", "status"}
            or manifest["format"] != "evernook-ordinary-archive" or type(manifest["version"]) is not int
            or manifest["version"] != 1 or type(manifest["storage_version"]) is not int or manifest["storage_version"] != 4):
        raise MemoryFailure("unsupported_archive")
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
        with (staging / "manifest.json").open("x", encoding="utf-8") as output_manifest:
            output_manifest.write(_json({"format": "evernook-ordinary-archive", "version": 1, "storage_version": 4,
                "created_at": datetime.now(timezone.utc).isoformat(), "status": asdict(status)}))
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


class EvidenceArchive:
    """Explicit archive-review boundary; no normal read/search or write operations."""

    def __init__(self, root: Path):
        self.root = external(root)
        verify_archive(self.root)

    def status(self) -> ArchiveStatus:
        return verify_archive(self.root)

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
