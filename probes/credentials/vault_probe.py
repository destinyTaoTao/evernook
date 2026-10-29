"""R0 synthetic credential feasibility probe. No service, production IPC or credential detection."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable

from nacl import pwhash, utils
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@dataclass(frozen=True)
class BoundObject:
    object_id: str
    version: int
    source: str
    key_id: str


@dataclass(frozen=True)
class Envelope:
    bound: BoundObject
    ciphertext: bytes
    signature: bytes

    def signed_content(self) -> bytes:
        return canonical({"format": 1, "bound": asdict(self.bound), "ciphertext": encoded(self.ciphertext)})

    def serialize(self) -> bytes:
        return canonical({"bound": asdict(self.bound), "ciphertext": encoded(self.ciphertext), "signature": encoded(self.signature)})

    @staticmethod
    def parse(value: bytes) -> Envelope:
        obj = json.loads(value)
        return Envelope(BoundObject(**obj["bound"]), base64.b64decode(obj["ciphertext"], validate=True), base64.b64decode(obj["signature"], validate=True))


def seal(recipient: PublicKey, sender: SigningKey, bound: BoundObject, value: bytes) -> Envelope:
    ciphertext = SealedBox(recipient).encrypt(canonical({"bound": asdict(bound), "value": encoded(value)}))
    unsigned = Envelope(bound, ciphertext, b"")
    return Envelope(bound, ciphertext, sender.sign(unsigned.signed_content()).signature)


def decrypt(recipient: PrivateKey, sender: VerifyKey, expected: BoundObject, envelope: Envelope) -> bytes:
    if envelope.bound != expected:
        raise ValueError("Object or source binding mismatch")
    sender.verify(envelope.signed_content(), envelope.signature)
    value = json.loads(SealedBox(recipient).decrypt(envelope.ciphertext))
    if value["bound"] != asdict(expected):
        raise ValueError("Encrypted object binding mismatch")
    return base64.b64decode(value["value"], validate=True)


def wrap_key(private: PrivateKey, password: bytes) -> bytes:
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    key = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, salt, opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
    ciphertext = SecretBox(key).encrypt(bytes(private))
    return canonical({"format": 1, "kdf": "argon2id-interactive", "salt": encoded(salt), "ciphertext": encoded(ciphertext)})


def unwrap_key(bundle: bytes, password: bytes) -> PrivateKey:
    value = json.loads(bundle)
    if value.get("format") != 1 or value.get("kdf") != "argon2id-interactive":
        raise ValueError("Unknown key protection format")
    key = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, base64.b64decode(value["salt"], validate=True),
                            opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
    return PrivateKey(SecretBox(key).decrypt(base64.b64decode(value["ciphertext"], validate=True)))


def external_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Use an absolute runtime path")
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Runtime evidence must remain outside checkout")
    return resolved


class Inbox:
    """Public/signing verification only; encrypted original and link recover in two steps."""

    def __init__(self, root: Path, sender: VerifyKey):
        self.sender = sender
        self.connection = sqlite3.connect(external_directory(root) / "inbox.sqlite", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS objects (id TEXT, version INTEGER, envelope BLOB NOT NULL, linked INTEGER NOT NULL, PRIMARY KEY(id,version))")

    def receive(self, envelope: Envelope) -> str:
        self.sender.verify(envelope.signed_content(), envelope.signature)
        with self.connection:
            stored = self.connection.execute("SELECT envelope FROM objects WHERE id=? AND version=?", (envelope.bound.object_id, envelope.bound.version)).fetchone()
            if stored and bytes(stored[0]) != envelope.serialize():
                raise ValueError("Source conflict")
            self.connection.execute("INSERT OR IGNORE INTO objects VALUES (?,?,?,0)", (envelope.bound.object_id, envelope.bound.version, envelope.serialize()))
        return "protected_pending_link"

    def link(self, bound: BoundObject) -> str:
        self._envelope(bound)
        with self.connection:
            self.connection.execute("UPDATE objects SET linked=1 WHERE id=? AND version=?", (bound.object_id, bound.version))
        return "protected_linked"

    def _envelope(self, bound: BoundObject) -> Envelope:
        row = self.connection.execute("SELECT envelope FROM objects WHERE id=? AND version=?", (bound.object_id, bound.version)).fetchone()
        if row is None:
            raise ValueError("Protected original unavailable")
        envelope = Envelope.parse(bytes(row[0]))
        self.sender.verify(envelope.signed_content(), envelope.signature)
        if envelope.bound != bound:
            raise ValueError("Source binding mismatch")
        return envelope

    def read(self, bound: BoundObject) -> Envelope:
        envelope = self._envelope(bound)
        row = self.connection.execute("SELECT linked FROM objects WHERE id=? AND version=?", (bound.object_id, bound.version)).fetchone()
        if row is None or not row[0]:
            raise ValueError("Protected original not linked")
        return envelope

    def recover_pending(self) -> int:
        rows = self.connection.execute("SELECT envelope FROM objects WHERE linked=0").fetchall()
        for row in rows:
            envelope = Envelope.parse(bytes(row[0]))
            self.link(envelope.bound)
        return len(rows)

    def latest_version(self, object_id: str) -> int:
        row = self.connection.execute("SELECT MAX(version) FROM objects WHERE id=?", (object_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else -1

    def backup(self, target: Path) -> None:
        external_directory(target.parent)
        if target.exists():
            raise ValueError("Backup path already exists")
        destination = sqlite3.connect(target)
        try:
            self.connection.backup(destination)
        finally:
            destination.close()

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class Request:
    bound: BoundObject
    client: str
    operation: str
    purpose: str


class Authority:
    """Single local helper experiment. Trusted pairing/confirmation are inputs, not inferred authorization."""

    def __init__(self, inbox: Inbox, private: PrivateKey, clients: dict[str, VerifyKey], clock: Callable[[], float] = time.monotonic):
        self.inbox = inbox
        self.private: PrivateKey | None = private
        self.clients = dict(clients)
        self.clock = clock
        self._grants: dict[str, tuple[Request, float]] = {}
        self._lock = threading.RLock()

    def grant_after_local_confirmation(self, request: Request, *, approved: bool, ttl: float = 30) -> str:
        with self._lock:
            if not approved or self.private is None or request.client not in self.clients or request.operation != "read" or not request.purpose or not 0 < ttl <= 120:
                raise PermissionError("Local approval denied")
            self.inbox.read(request.bound)
            if self.inbox.latest_version(request.bound.object_id) != request.bound.version:
                raise PermissionError("Credential version superseded")
            token = secrets.token_urlsafe(32)
            self._grants[token] = (request, self.clock() + ttl)
            return token

    @staticmethod
    def claim(token: str, request: Request) -> bytes:
        return canonical({"token": token, "request": asdict(request)})

    def deliver(self, token: str, request: Request, signature: bytes) -> bytes:
        with self._lock:
            grant = self._grants.get(token)
            if self.private is None or grant is None or grant[0] != request or self.clock() >= grant[1]:
                raise PermissionError("Grant unavailable")
            if self.inbox.latest_version(request.bound.object_id) != request.bound.version:
                raise PermissionError("Credential version superseded")
            client = self.clients.get(request.client)
            if client is None:
                raise PermissionError("Unregistered caller")
            client.verify(self.claim(token, request), signature)
            del self._grants[token]  # At most once, even if downstream decryption fails.
            return decrypt(self.private, self.inbox.sender, request.bound, self.inbox.read(request.bound))

    def revoke(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)

    def lock(self) -> None:
        with self._lock:
            self._grants.clear()
            self.private = None  # Python cannot promise zeroization of all historical object copies.
