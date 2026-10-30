"""Opt-in encrypted inbox. Ordinary workers hold receiving/signing keys, never a decrypting key."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import UUID, uuid4

from nacl import pwhash, utils
from nacl.exceptions import BadSignatureError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey

from .models import Access, Event, MemoryFailure, Receipt, Source
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _json, _load, _unique_object


@dataclass(frozen=True)
class ProtectedReceipt:
    inbox_id: str
    object_id: str
    key_id: str
    version: int
    state: str
    message: Receipt | None
    encrypted_original_saved: bool = True


def _write(path: Path, value: bytes) -> None:
    with path.open("xb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _hex(value: object, length: int) -> bytes:
    if not isinstance(value, str) or len(value) != length * 2:
        raise MemoryFailure("invalid_protected_material")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise MemoryFailure("invalid_protected_material") from None


def _public(path: Path) -> tuple[dict[str, Any], PublicKey, VerifyKey]:
    verify_private_directory(path.parent, extra_files=(path,))
    value = _load(path)
    if (set(value) != {"format", "version", "key_id", "recipient", "sender"}
            or value["format"] != "evernook-protected-receiver" or type(value["version"]) is not int or value["version"] != 1):
        raise MemoryFailure("invalid_protected_material")
    recipient, sender = PublicKey(_hex(value["recipient"], 32)), VerifyKey(_hex(value["sender"], 32))
    expected = hashlib.sha256(b"evernook-protected-v1" + bytes(recipient) + bytes(sender)).hexdigest()
    if value["key_id"] != expected:
        raise MemoryFailure("invalid_protected_material")
    return value, recipient, sender


def initialize_protected_inbox(runtime: MemoryRuntime, root: Path, key_root: Path, password: bytes) -> ProtectedInbox:
    """Trusted local initialization only. Neither this operation nor passwords are model tools."""
    if not isinstance(password, bytes) or not 12 <= len(password) <= 1024:
        raise MemoryFailure("invalid_vault_password")
    root, key_root = external(root), external(key_root)
    if (root == key_root or root.is_relative_to(key_root) or key_root.is_relative_to(root)
            or root.is_relative_to(runtime.root) or key_root.is_relative_to(runtime.root)
            or runtime.root.is_relative_to(root) or runtime.root.is_relative_to(key_root)):
        raise MemoryFailure("protected_directories_must_be_separate")
    if root.exists() or key_root.exists():
        raise MemoryFailure("protected_directory_exists")
    provision_private_directory(key_root)
    private, sender = PrivateKey.generate(), SigningKey.generate()
    key_id = hashlib.sha256(b"evernook-protected-v1" + bytes(private.public_key) + bytes(sender.verify_key)).hexdigest()
    public = {"format": "evernook-protected-receiver", "version": 1, "key_id": key_id,
              "recipient": bytes(private.public_key).hex(), "sender": bytes(sender.verify_key).hex()}
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, salt,
        opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
    recovery = _json({"format": "evernook-protected-private", "version": 1,
                     "public": public, "private": bytes(private).hex()}).encode()
    _write(key_root / "receiving.json", _json(public).encode())
    _write(key_root / "writer.key", bytes(sender))
    _write(key_root / "recovery.json", _json({"format": "evernook-protected-recovery", "version": 1,
        "kdf": "argon2id-interactive-v1", "salt": salt.hex(), "ciphertext": SecretBox(derived).encrypt(recovery).hex()}).encode())
    provision_private_directory(root)
    db = sqlite3.connect(root / "inbox.sqlite3", isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE settings (singleton INTEGER PRIMARY KEY CHECK(singleton=1), inbox_id TEXT NOT NULL, public TEXT NOT NULL, runtime_binding BLOB NOT NULL)")
        db.execute("""CREATE TABLE objects (id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL,
            fingerprint BLOB NOT NULL, envelope TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT)""")
        binding = hmac.digest(runtime._fingerprint_key, b"evernook-protected-inbox-v1", hashlib.sha256)
        db.execute("INSERT INTO settings VALUES (1,?,?,?)", (str(uuid4()), _json(public), binding))
        db.execute("COMMIT")
    finally:
        db.close()
    return ProtectedInbox(runtime, root, key_root)


class ProtectedInbox:
    """Two durable stores: stage ciphertext, link the ordinary projection, then acknowledge linkage."""

    def __init__(self, runtime: MemoryRuntime, root: Path, key_root: Path):
        self.runtime, self.root, self.key_root = runtime, external(root), external(key_root)
        self.public, self.recipient, self.sender = _public(self.key_root / "receiving.json")
        with self._connection() as db:
            row = db.execute("SELECT * FROM settings WHERE singleton=1").fetchone()
            binding = hmac.digest(runtime._fingerprint_key, b"evernook-protected-inbox-v1", hashlib.sha256)
            if row is None or json.loads(row["public"]) != self.public or not hmac.compare_digest(bytes(row["runtime_binding"]), binding):
                raise MemoryFailure("protected_binding_mismatch")
            self.inbox_id = str(row["inbox_id"])

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        verify_private_directory(self.root, extra_files=(self.root / "inbox.sqlite3",))
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(self.root / "inbox.sqlite3", timeout=1, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 255
            raise MemoryFailure({sqlite3.SQLITE_BUSY: "storage_busy", sqlite3.SQLITE_LOCKED: "storage_busy",
                sqlite3.SQLITE_FULL: "storage_full"}.get(code, "protected_storage_unavailable")) from None
        finally:
            if db is not None:
                db.close()

    def _writer(self) -> SigningKey:
        public, _, _ = _public(self.key_root / "receiving.json")
        verify_private_directory(self.key_root, extra_files=(self.key_root / "writer.key",))
        if public != self.public:
            raise MemoryFailure("protected_binding_mismatch")
        try:
            sender = SigningKey((self.key_root / "writer.key").read_bytes())
        except (OSError, ValueError):
            raise MemoryFailure("invalid_protected_material") from None
        if bytes(sender.verify_key) != bytes(self.sender):
            raise MemoryFailure("protected_binding_mismatch")
        return sender

    def _verified(self, row: sqlite3.Row) -> tuple[dict[str, Any], Event]:
        try:
            value = json.loads(row["envelope"], object_pairs_hook=_unique_object)
            if not isinstance(value, dict) or set(value) != {"bound", "ciphertext", "signature"}:
                raise ValueError()
            bound = value["bound"]
            if (set(bound) != {"inbox_id", "object_id", "key_id", "version", "projection", "fingerprint"}
                    or bound["inbox_id"] != self.inbox_id or bound["object_id"] != row["id"]
                    or bound["key_id"] != self.public["key_id"] or type(bound["version"]) is not int or bound["version"] != 1
                    or bound["fingerprint"] != bytes(row["fingerprint"]).hex()):
                raise ValueError()
            cipher = bytes.fromhex(value["ciphertext"])
            if not 48 <= len(cipher) <= 2 * 1048576:
                raise ValueError()
            self.sender.verify(_json({"bound": bound, "ciphertext": value["ciphertext"]}).encode(), _hex(value["signature"], 64))
            event_value = dict(bound["projection"])
            event_value["source"] = Source(**event_value["source"])
            event = Event(**event_value)
            self.runtime._validate_event(event)
            if (_json(asdict(event.source)) != row["source"] or event.text != "[PROTECTED:ENCRYPTED_ORIGINAL]"
                    or not event.protected_source):
                raise ValueError()
            return value, event
        except (ValueError, TypeError, KeyError, BadSignatureError):
            raise MemoryFailure("protected_object_corrupt") from None

    def _receipt(self, row: sqlite3.Row) -> ProtectedReceipt:
        self._verified(row)
        if (row["state"] not in {"protected_pending_link", "protected_linked"}
                or (row["state"] == "protected_linked") != (row["receipt"] is not None)):
            raise MemoryFailure("protected_object_corrupt")
        message = None
        if row["state"] == "protected_linked":
            # Inbox acknowledgement alone is insufficient after partial recovery/corruption.
            with self.runtime._connection() as core:
                if not core.execute("SELECT 1 FROM sqlite_master WHERE name='protected_links_v1'").fetchone():
                    raise MemoryFailure("protected_link_unavailable")
                saved = core.execute("SELECT messages.receipt, messages.fingerprint, protected_links_v1.reference FROM messages JOIN protected_links_v1 ON protected_links_v1.message_id=messages.id WHERE source=?",
                                     (row["source"],)).fetchone()
                if (saved is None or saved["receipt"] != row["receipt"]
                        or not hmac.compare_digest(bytes(saved["fingerprint"]), bytes(row["fingerprint"]))
                        or saved["reference"] != self._reference(str(row["id"]), str(row["envelope"]))):
                    raise MemoryFailure("protected_link_unavailable")
                message = Receipt(**json.loads(saved["receipt"]))
        return ProtectedReceipt(self.inbox_id, str(row["id"]), str(self.public["key_id"]), 1, str(row["state"]),
                                message)

    def _reference(self, object_id: str, envelope: str) -> str:
        return _json({"inbox_id": self.inbox_id, "object_id": object_id, "version": 1,
                      "key_id": self.public["key_id"], "envelope_sha256": hashlib.sha256(envelope.encode()).hexdigest()})

    def stage(self, access: Access, event: Event) -> ProtectedReceipt:
        identity = self.runtime._authorize(access)
        self.runtime._validate_event(event)
        if event.source.entry != identity.entry:
            raise MemoryFailure("unauthorized_source")
        fingerprint = hmac.digest(self.runtime._fingerprint_key, b"protected-event-v1" + _json(asdict(event)).encode(), hashlib.sha256)
        source = _json(asdict(event.source))
        sender = self._writer()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM objects WHERE source=?", (source,)).fetchone()
            if previous is not None:
                if not hmac.compare_digest(bytes(previous["fingerprint"]), fingerprint):
                    raise MemoryFailure("source_conflict")
                return self._receipt(previous)
            object_id = str(uuid4())
            projection = replace(event, text="[PROTECTED:ENCRYPTED_ORIGINAL]", protected_source=True)
            bound = {"inbox_id": self.inbox_id, "object_id": object_id, "key_id": self.public["key_id"], "version": 1,
                     "projection": asdict(projection), "fingerprint": fingerprint.hex()}
            try:
                cipher = SealedBox(self.recipient).encrypt(_json({"bound": bound, "event": asdict(event)}).encode())
                unsigned = {"bound": bound, "ciphertext": cipher.hex()}
                envelope = {**unsigned, "signature": sender.sign(_json(unsigned).encode()).signature.hex()}
            except Exception:
                raise MemoryFailure("protected_encryption_failed") from None
            db.execute("INSERT INTO objects VALUES (?,?,?,?,'protected_pending_link',NULL)",
                       (object_id, source, fingerprint, _json(envelope)))
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
            result = self._receipt(row)
            db.execute("COMMIT")
        return result

    def reconcile(self, access: Access, object_id: str) -> ProtectedReceipt:
        identity = self.runtime._authorize(access)
        try:
            if str(UUID(object_id)) != object_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MemoryFailure("invalid_protected_id") from None
        with self._connection() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
            if row is None:
                raise MemoryFailure("protected_object_not_found")
            _, projection = self._verified(row)
            envelope = str(row["envelope"])
            fingerprint = bytes(row["fingerprint"])
        if projection.source.entry != identity.entry:
            raise MemoryFailure("unauthorized_source")
        reference = self._reference(object_id, envelope)
        with self.runtime._connection() as core:
            core.execute("BEGIN IMMEDIATE")
            receipt = self.runtime._persist_event(core, projection, replace(projection), fingerprint)
            core.execute("CREATE TABLE IF NOT EXISTS protected_links_v1 (message_id TEXT PRIMARY KEY REFERENCES messages(id), reference TEXT NOT NULL)")
            previous = core.execute("SELECT reference FROM protected_links_v1 WHERE message_id=?", (receipt.message_id,)).fetchone()
            if previous is not None and previous["reference"] != reference:
                raise MemoryFailure("protected_link_conflict")
            core.execute("INSERT OR IGNORE INTO protected_links_v1 VALUES (?,?)", (receipt.message_id, reference))
            core.execute("COMMIT")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
            self._verified(current)
            if current["envelope"] != envelope:
                raise MemoryFailure("protected_object_corrupt")
            db.execute("UPDATE objects SET state='protected_linked',receipt=? WHERE id=?", (_json(asdict(receipt)), object_id))
            result = self._receipt(db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone())
            db.execute("COMMIT")
        return result

    def submit(self, access: Access, event: Event) -> ProtectedReceipt:
        staged = self.stage(access, event)
        return self.reconcile(access, staged.object_id)

    def status(self, access: Access, object_id: str) -> ProtectedReceipt:
        identity = self.runtime._authorize(access)
        with self._connection() as db:
            row = db.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
            if row is None:
                raise MemoryFailure("protected_object_not_found")
            _, projection = self._verified(row)
            if projection.source.entry != identity.entry:
                raise MemoryFailure("unauthorized_source")
            return self._receipt(row)

    def pending(self, access: Access, *, limit: int = 100) -> tuple[ProtectedReceipt, ...]:
        identity = self.runtime._authorize(access)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoryFailure("invalid_page")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM objects WHERE state='protected_pending_link' AND json_extract(source,'$.entry')=? ORDER BY id LIMIT ?",
                              (identity.entry, limit)).fetchall()
            return tuple(self._receipt(row) for row in rows)
