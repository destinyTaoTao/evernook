"""Explicit encrypted snapshots; recovery defaults to archive review, never stale recall."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from nacl import bindings, pwhash, utils
from nacl.exceptions import CryptoError, BadSignatureError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey

from .archive import EvidenceArchive, verify_archive
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _identities, _json, _load, _time, _unique_object


_CHUNK = 1024 * 1024
_FILES = {"manifest": "manifest.json", "evidence": "evidence.sqlite3", "runtime": "runtime-materials.json"}


@dataclass(frozen=True)
class BackupKeyInfo:
    key_id: str
    public_file: str
    recovery_file: str


@dataclass(frozen=True)
class BackupStatus:
    backup_id: str
    key_id: str
    created_at: str
    recovery_state: str = "review_required"
    schedule_enabled: bool = False


@dataclass(frozen=True)
class RetentionResult:
    kept: tuple[str, ...]
    eligible_for_removal: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[str, ...]
    recovery_anchor: str


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _write(path: Path, value: bytes) -> None:
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _password(password: bytes) -> None:
    if not isinstance(password, bytes) or not 12 <= len(password) <= 1024:
        raise MemoryFailure("invalid_recovery_password")


def _json_bytes(value: bytes, maximum: int = 1048576) -> dict[str, Any]:
    if len(value) > maximum:
        raise MemoryFailure("invalid_backup")
    try:
        result: object = json.loads(value.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise MemoryFailure("invalid_backup") from None
    if not isinstance(result, dict):
        raise MemoryFailure("invalid_backup")
    return result


def _hex(value: object, length: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > 65536:
        raise MemoryFailure("invalid_backup")
    try:
        result = bytes.fromhex(value)
    except ValueError:
        raise MemoryFailure("invalid_backup") from None
    if length is not None and len(result) != length:
        raise MemoryFailure("invalid_backup")
    return result


def _key_id(recipient: bytes, sender: bytes) -> str:
    return hashlib.sha256(recipient + sender).hexdigest()


def initialize_backup_keys(root: Path, password: bytes) -> BackupKeyInfo:
    """Trusted local operation. Passwords are never a CLI/MCP argument or environment value."""
    _password(password)
    root = external(root)
    if root.exists():
        raise MemoryFailure("backup_keys_exist")
    provision_private_directory(root)
    private = PrivateKey.generate()
    sender = SigningKey.generate()
    key_id = _key_id(bytes(private.public_key), bytes(sender.verify_key))
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, salt,
        opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
    payload = _json({"key_id": key_id, "private": bytes(private).hex(), "sender_public": bytes(sender.verify_key).hex()}).encode()
    wrapped = SecretBox(derived).encrypt(payload)
    _write(root / "receiving.json", _json({"schema_version": 1, "key_id": key_id,
        "recipient_public": bytes(private.public_key).hex(), "sender_public": bytes(sender.verify_key).hex()}).encode())
    _write(root / "writer.key", bytes(sender))
    _write(root / "recovery.json", _json({"schema_version": 1, "kdf": "argon2id-interactive-v1",
        "salt": salt.hex(), "ciphertext": wrapped.hex()}).encode())
    return BackupKeyInfo(key_id, str(root / "receiving.json"), str(root / "recovery.json"))


def _writer(root: Path) -> tuple[str, PublicKey, SigningKey]:
    root = external(root)
    verify_private_directory(root, extra_files=tuple(root / name for name in ("receiving.json", "writer.key")))
    info = _load(root / "receiving.json")
    if set(info) != {"schema_version", "key_id", "recipient_public", "sender_public"} or type(info["schema_version"]) is not int or info["schema_version"] != 1:
        raise MemoryFailure("invalid_backup_keys")
    try:
        recipient = PublicKey(_hex(info["recipient_public"], 32))
        raw = (root / "writer.key").read_bytes()
        if len(raw) != 32:
            raise MemoryFailure("invalid_backup_keys")
        sender = SigningKey(raw)
        key_id = _key_id(bytes(recipient), bytes(sender.verify_key))
        if info["key_id"] != key_id or info["sender_public"] != bytes(sender.verify_key).hex():
            raise MemoryFailure("invalid_backup_keys")
        return key_id, recipient, sender
    except (ValueError, OSError):
        raise MemoryFailure("invalid_backup_keys") from None


def _unlock(path: Path, password: bytes) -> tuple[str, PrivateKey, VerifyKey]:
    _password(password)
    path = external(path)
    verify_private_directory(path.parent, extra_files=(path,))
    try:
        with path.open("rb") as input_file:
            wrapped = _json_bytes(input_file.read(65537), 65536)
        if (set(wrapped) != {"schema_version", "kdf", "salt", "ciphertext"} or type(wrapped["schema_version"]) is not int
                or wrapped["schema_version"] != 1 or wrapped["kdf"] != "argon2id-interactive-v1"):
            raise MemoryFailure("invalid_backup_keys")
        derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, _hex(wrapped["salt"], pwhash.argon2id.SALTBYTES),
            opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        value = _json_bytes(SecretBox(derived).decrypt(_hex(wrapped["ciphertext"])), 4096)
        if set(value) != {"key_id", "private", "sender_public"}:
            raise MemoryFailure("invalid_backup_keys")
        private, sender = PrivateKey(_hex(value["private"], 32)), VerifyKey(_hex(value["sender_public"], 32))
        key_id = _key_id(bytes(private.public_key), bytes(sender))
        if value["key_id"] != key_id:
            raise MemoryFailure("invalid_backup_keys")
        return key_id, private, sender
    except (CryptoError, ValueError, OSError):
        raise MemoryFailure("recovery_unlock_failed") from None


def _encrypt(source: BinaryIO, destination: Path, recipient: PublicKey, aad: bytes) -> dict[str, object]:
    key = bindings.crypto_secretstream_xchacha20poly1305_keygen()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    header = bindings.crypto_secretstream_xchacha20poly1305_init_push(state, key)
    size = 0
    with destination.open("xb") as output:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            encrypted = bindings.crypto_secretstream_xchacha20poly1305_push(state, chunk, aad)
            output.write(struct.pack(">I", len(encrypted)))
            output.write(encrypted)
        final = bindings.crypto_secretstream_xchacha20poly1305_push(state, b"", aad,
            bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL)
        output.write(struct.pack(">I", len(final)))
        output.write(final)
        output.flush()
        os.fsync(output.fileno())
    return {"stream_header": header.hex(), "wrapped_key": SealedBox(recipient).encrypt(key).hex(),
            "sha256": _digest(destination), "plaintext_size": size}


def _decrypt(source: Path, output: BinaryIO, private: PrivateKey, info: dict[str, Any], aad: bytes) -> None:
    if set(info) != {"stream_header", "wrapped_key", "sha256", "plaintext_size"}:
        raise MemoryFailure("invalid_backup")
    if type(info["plaintext_size"]) is not int or info["plaintext_size"] < 0 or _digest(source) != info["sha256"]:
        raise MemoryFailure("backup_integrity_mismatch")
    key = SealedBox(private).decrypt(_hex(info["wrapped_key"], 80))
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    bindings.crypto_secretstream_xchacha20poly1305_init_pull(state,
        _hex(info["stream_header"], bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES), key)
    size = 0
    with source.open("rb") as input_file:
        while True:
            prefix = input_file.read(4)
            if len(prefix) != 4:
                raise MemoryFailure("backup_truncated")
            length = struct.unpack(">I", prefix)[0]
            if not bindings.crypto_secretstream_xchacha20poly1305_ABYTES <= length <= _CHUNK + bindings.crypto_secretstream_xchacha20poly1305_ABYTES:
                raise MemoryFailure("invalid_backup")
            chunk = input_file.read(length)
            if len(chunk) != length:
                raise MemoryFailure("backup_truncated")
            plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, chunk, aad)
            if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
                if plaintext or input_file.read(1) or size != info["plaintext_size"]:
                    raise MemoryFailure("backup_integrity_mismatch")
                return
            if tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE:
                raise MemoryFailure("invalid_backup")
            size += len(plaintext)
            if size > info["plaintext_size"]:
                raise MemoryFailure("backup_integrity_mismatch")
            output.write(plaintext)


def _create_backup(runtime: MemoryRuntime, access: Access, destination: Path, key_directory: Path) -> BackupStatus:
    identity = runtime._authorize(access)
    if not identity.archive_review:
        raise MemoryFailure("archive_forbidden")
    destination = external(destination)
    key_directory = external(key_directory)
    if (destination.is_relative_to(runtime.root) or key_directory.is_relative_to(destination)
            or destination.is_relative_to(key_directory) or key_directory.is_relative_to(runtime.root)):
        raise MemoryFailure("unsafe_backup_path")
    if destination.exists():
        raise MemoryFailure("backup_destination_exists")
    key_id, recipient, sender = _writer(key_directory)
    backup_id = str(uuid4())
    status = BackupStatus(backup_id, key_id, datetime.now(timezone.utc).isoformat())
    # Plaintext staging is owner-only and outside the destination; ciphertext alone is published.
    with tempfile.TemporaryDirectory(prefix=".evernook-backup-", dir=runtime.root.parent) as temporary:
        work = Path(temporary) / "work"
        provision_private_directory(work)
        before = (runtime.root / "idempotency.key").read_bytes()
        runtime.export_archive(access, work / "archive")
        clients = _load(runtime.root / "client.json")
        _identities(clients)
        if before != (runtime.root / "idempotency.key").read_bytes():
            raise MemoryFailure("backup_materials_changed")
        _write(work / "runtime-materials.json", _json({"schema_version": 1, "clients": clients,
            "idempotency_key": before.hex()}).encode())
        with tempfile.TemporaryDirectory(prefix=".evernook-encrypted-", dir=destination.parent) as encrypted_temporary:
            staging = Path(encrypted_temporary) / "backup"
            provision_private_directory(staging)
            files = {}
            for name, filename in _FILES.items():
                source = work / filename if name == "runtime" else work / "archive" / filename
                aad = _json({"backup_id": backup_id, "key_id": key_id, "file": name}).encode()
                with source.open("rb") as input_file:
                    files[name] = _encrypt(input_file, staging / (name + ".enc"), recipient, aad)
            content = {"format": "evernook-encrypted-backup", "version": 1, "status": asdict(status), "files": files}
            _write(staging / "backup.json", _json({"content": content, "signature": sender.sign(_json(content).encode()).signature.hex()}).encode())
            if not runtime._authorize(access).archive_review:
                raise MemoryFailure("archive_forbidden")
            os.rename(staging, destination)
    return status


def create_backup(runtime: MemoryRuntime, access: Access, destination: Path, key_directory: Path) -> BackupStatus:
    try:
        return _create_backup(runtime, access, destination, key_directory)
    except (OSError, ValueError, TypeError, KeyError):
        raise MemoryFailure("backup_write_failed") from None


def _verified_header(root: Path, key_id: str, sender: VerifyKey) -> tuple[BackupStatus, dict[str, Any]]:
    root = external(root)
    paths = tuple(root / name for name in ("backup.json", "manifest.enc", "evidence.enc", "runtime.enc"))
    if {path.name for path in root.iterdir()} != {path.name for path in paths}:
        raise MemoryFailure("invalid_backup")
    verify_private_directory(root, extra_files=paths)
    with (root / "backup.json").open("rb") as input_file:
        header = _json_bytes(input_file.read(65537), 65536)
    if set(header) != {"content", "signature"} or not isinstance(header["content"], dict):
        raise MemoryFailure("invalid_backup")
    content = header["content"]
    sender.verify(_json(content).encode(), _hex(header["signature"], 64))
    if (set(content) != {"format", "version", "status", "files"} or content["format"] != "evernook-encrypted-backup"
            or type(content["version"]) is not int or content["version"] != 1):
        raise MemoryFailure("unsupported_backup")
    status = content["status"]
    if (not isinstance(status, dict) or set(status) != set(BackupStatus.__dataclass_fields__) or status["key_id"] != key_id
            or status["recovery_state"] != "review_required" or status["schedule_enabled"] is not False
            or not isinstance(content["files"], dict) or set(content["files"]) != set(_FILES)):
        raise MemoryFailure("invalid_backup")
    if not isinstance(status["backup_id"], str) or not isinstance(status["created_at"], str):
        raise MemoryFailure("invalid_backup")
    UUID(status["backup_id"])
    _time(status["created_at"])
    return BackupStatus(**status), content["files"]


def restore_backup(source: Path, destination: Path, recovery_file: Path, password: bytes) -> EvidenceArchive:
    """Authenticated encrypted recovery. A snapshot never proves later revocations complete."""
    source, destination = external(source), external(destination)
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise MemoryFailure("unsafe_backup_path")
    if destination.exists():
        raise MemoryFailure("backup_destination_exists")
    try:
        key_id, private, sender = _unlock(recovery_file, password)
        status, files = _verified_header(source, key_id, sender)
        with tempfile.TemporaryDirectory(prefix=".evernook-restore-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "archive"
            provision_private_directory(staging)
            for name, filename in _FILES.items():
                info = files[name]
                if not isinstance(info, dict):
                    raise MemoryFailure("invalid_backup")
                if name != "evidence" and (type(info.get("plaintext_size")) is not int or not 0 <= info["plaintext_size"] <= 1048576):
                    raise MemoryFailure("invalid_backup")
                aad = _json({"backup_id": status.backup_id, "key_id": key_id, "file": name}).encode()
                if name == "runtime":
                    # Do not publish keys/tokens to an ordinary archive or silently resume old identities.
                    import io
                    output_memory = io.BytesIO()
                    _decrypt(source / (name + ".enc"), output_memory, private, info, aad)
                    materials = _json_bytes(output_memory.getvalue())
                    if (set(materials) != {"schema_version", "clients", "idempotency_key"}
                            or type(materials["schema_version"]) is not int or materials["schema_version"] != 1):
                        raise MemoryFailure("invalid_backup")
                    _identities(materials["clients"])
                    _hex(materials["idempotency_key"], 32)
                else:
                    with (staging / filename).open("xb") as output_file:
                        _decrypt(source / (name + ".enc"), output_file, private, info, aad)
                        output_file.flush()
                        os.fsync(output_file.fileno())
            verify_archive(staging)
            os.rename(staging, destination)
        return EvidenceArchive(destination)
    except (CryptoError, BadSignatureError):
        raise MemoryFailure("backup_authentication_failed") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise MemoryFailure("invalid_backup") from None


def retain_backups(root: Path, key_directory: Path, *, recovery_verified_id: str,
                   now: datetime | None = None, apply: bool = False) -> RetentionResult:
    """Trusted local maintenance. Retain a user-verified recovery anchor plus rolling hourly/daily copies."""
    root, key_directory = external(root), external(key_directory)
    if root.is_relative_to(key_directory) or key_directory.is_relative_to(root):
        raise MemoryFailure("unsafe_backup_path")
    verify_private_directory(root)
    if type(apply) is not bool or not isinstance(recovery_verified_id, str):
        raise MemoryFailure("invalid_retention_request")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise MemoryFailure("invalid_retention_request")
    key_id, _, sender = _writer(key_directory)
    snapshots: list[tuple[Path, BackupStatus, str, float]] = []
    skipped: list[str] = []
    for path in sorted(root.iterdir()):
        resolved = path.resolve()
        if not path.is_dir() or resolved.parent != root:
            skipped.append(path.name)
            continue
        try:
            status, files = _verified_header(resolved, key_id, sender.verify_key)
            if any(not isinstance(info, dict) or _digest(resolved / (name + ".enc")) != info.get("sha256")
                   for name, info in files.items()):
                raise MemoryFailure("backup_integrity_mismatch")
            age = current.timestamp() - _time(status.created_at)
            if age < 0:
                skipped.append(path.name)
                continue
            snapshots.append((resolved, status, _digest(resolved / "backup.json"), age))
        except (MemoryFailure, CryptoError, ValueError, OSError, TypeError, KeyError):
            # Foreign keys, unsupported/corrupt bundles and links are never automatically deleted.
            skipped.append(path.name)
    if not any(status.backup_id == recovery_verified_id for _, status, _, _ in snapshots):
        raise MemoryFailure("verified_recovery_anchor_required")
    snapshots.sort(key=lambda item: item[3])
    keep = {recovery_verified_id}
    if snapshots:
        keep.add(snapshots[0][1].backup_id)
    hours: set[int] = set()
    days: set[int] = set()
    for _, status, _, age in snapshots:
        hour, day = int(age // 3600), int(age // 86400)
        if hour < 48 and hour not in hours:
            keep.add(status.backup_id)
            hours.add(hour)
        if day < 30 and day not in days:
            keep.add(status.backup_id)
            days.add(day)
    eligible = [item for item in snapshots if item[1].backup_id not in keep]
    removed: list[str] = []
    if apply:
        for path, status, manifest_hash, _ in eligible:
            try:
                if path.resolve().parent != root or _digest(path / "backup.json") != manifest_hash:
                    raise MemoryFailure("backup_changed_during_retention")
                _verified_header(path, key_id, sender.verify_key)
                shutil.rmtree(path)
            except (MemoryFailure, CryptoError, ValueError, OSError):
                # A bundle that changed or cannot be removed is never deleted; it is reported as
                # skipped so one stuck snapshot cannot abort the whole retention run mid-report.
                skipped.append(path.name)
                continue
            removed.append(status.backup_id)
    return RetentionResult(tuple(status.backup_id for _, status, _, _ in snapshots if status.backup_id in keep),
                           tuple(item[1].backup_id for item in eligible), tuple(removed), tuple(skipped), recovery_verified_id)
