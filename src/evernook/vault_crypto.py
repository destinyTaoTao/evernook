"""Trusted local recovery primitives; never loaded by ordinary retrieval."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import hmac
import json
import sqlite3

from nacl import pwhash
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox

from .models import Event, MemoryFailure, Source
from .paths import verify_private_directory
from .protected import ProtectedInbox, _hex, _public
from .runtime import _json, _unique_object


def _json_object(raw: bytes) -> dict[str, object]:
    if len(raw) > 65536:
        raise MemoryFailure('invalid_vault_recovery')
    try:
        value: object = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise MemoryFailure('invalid_vault_recovery') from None
    if not isinstance(value, dict):
        raise MemoryFailure('invalid_vault_recovery')
    return value


def _unlock(inbox: ProtectedInbox, password: bytes) -> PrivateKey:
    if not isinstance(password, bytes) or not 12 <= len(password) <= 1024:
        raise MemoryFailure('vault_unlock_refused')
    path = inbox.key_root / 'recovery.json'
    verify_private_directory(inbox.key_root, extra_files=(path,))
    public, _, _ = _public(inbox.key_root / 'receiving.json')
    if public != inbox.public:
        raise MemoryFailure('protected_binding_mismatch')
    try:
        with path.open('rb') as source:
            value = _json_object(source.read(65537))
        if (set(value) != {'format', 'version', 'kdf', 'salt', 'ciphertext'}
                or value['format'] != 'evernook-protected-recovery' or type(value['version']) is not int or value['version'] != 1
                or value['kdf'] != 'argon2id-interactive-v1' or not isinstance(value['ciphertext'], str)
                or len(value['ciphertext']) > 32768):
            raise MemoryFailure('invalid_vault_recovery')
        derived = pwhash.argon2id.kdf(SecretBox.KEY_SIZE, password, _hex(value['salt'], pwhash.argon2id.SALTBYTES),
            opslimit=pwhash.argon2id.OPSLIMIT_INTERACTIVE, memlimit=pwhash.argon2id.MEMLIMIT_INTERACTIVE)
        payload = _json_object(SecretBox(derived).decrypt(bytes.fromhex(value['ciphertext'])))
        if (set(payload) != {'format', 'version', 'public', 'private'} or payload['format'] != 'evernook-protected-private'
                or type(payload['version']) is not int or payload['version'] != 1 or payload['public'] != inbox.public):
            raise MemoryFailure('invalid_vault_recovery')
        private = PrivateKey(_hex(payload['private'], 32))
        if bytes(private.public_key) != bytes(inbox.recipient):
            raise MemoryFailure('protected_binding_mismatch')
        return private
    except (CryptoError, ValueError, OSError):
        raise MemoryFailure('vault_unlock_refused') from None


def _decrypt_event(inbox: ProtectedInbox, row: sqlite3.Row, private: PrivateKey, *,
                   expected_envelope: str | None = None, expected_bound: str | None = None) -> Event:
    value, projection = inbox._verified(row)
    bound = _json(value['bound'])
    if ((expected_envelope is not None and row['envelope'] != expected_envelope)
            or (expected_bound is not None and bound != expected_bound)):
        raise MemoryFailure('vault_request_changed')
    try:
        clear = json.loads(SealedBox(private).decrypt(bytes.fromhex(value['ciphertext'])), object_pairs_hook=_unique_object)
        version = value['bound']['version']
        expected_fields = {'bound', 'event', 'provenance'} if version == 2 else {'bound', 'event'}
        provenance = {'format': 'buffered_text_stream', 'gaps': ['private_stream_boundaries_not_saved']}
        if (not isinstance(clear, dict) or set(clear) != expected_fields or _json(clear['bound']) != bound
                or (version == 2 and clear['provenance'] != provenance)):
            raise ValueError()
        event_value = dict(clear['event'])
        event_value['source'] = Source(**event_value['source'])
        event = Event(**event_value)
        inbox.runtime._validate_event(event)
        if version == 2 and (event.sensitive is not True or event.completion != 'complete' or event.role == 'tool'):
            raise ValueError()
        fingerprint_data = (b'protected-event-v2' + _json({'event': asdict(event), 'provenance': provenance}).encode()
                            if version == 2 else b'protected-event-v1' + _json(asdict(event)).encode())
        actual = hmac.digest(inbox.runtime._fingerprint_key, fingerprint_data, hashlib.sha256)
        if (not hmac.compare_digest(actual, bytes(row['fingerprint']))
                or replace(event, text='[PROTECTED:ENCRYPTED_ORIGINAL]', protected_source=True) != projection):
            raise ValueError()
        return event
    except (CryptoError, ValueError, TypeError, KeyError):
        raise MemoryFailure('protected_object_corrupt') from None
