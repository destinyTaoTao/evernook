"""Authenticated service binding for a declared multi-inbox set."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .models import Access, MemoryFailure
from .paths import verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_json, _json, _load, _unique_object
from .vault_migration import _file_bytes
from .vault_multi_proof import _check
from .vault_multi_verify import _inspect


BINDING_NAME = 'protected-multi-binding.json'


def _auth(runtime: MemoryRuntime, content: dict[str, Any]) -> str:
    return hmac.digest(runtime._fingerprint_key, b'protected-multi-binding-v1\0'
                       + _json(content).encode(), hashlib.sha256).hex()


def read(runtime: MemoryRuntime) -> dict[str, Any] | None:
    path = runtime.root / BINDING_NAME
    verify_private_directory(runtime.root, extra_files=(path,))
    if not path.exists():
        return None
    try:
        value: object = json.loads(_file_bytes(path, 65536).decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {'content', 'authentication'} or not isinstance(value['content'], dict):
            raise ValueError()
        content = value['content']
        authentication = value['authentication']
        if (set(content) != {'version', 'ledger_id', 'primary', 'inboxes'}
                or type(content['version']) is not int or content['version'] != 1
                or not isinstance(content['ledger_id'], str) or not isinstance(content['primary'], str)
                or not isinstance(content['inboxes'], list) or not 2 <= len(content['inboxes']) <= 16
                or not isinstance(authentication, str) or len(authentication) != 64
                or not hmac.compare_digest(authentication, _auth(runtime, content))):
            raise ValueError()
        return content
    except (ValueError, TypeError, KeyError, UnicodeError, MemoryFailure):
        raise MemoryFailure('protected_multi_binding_invalid') from None


def _content(snapshot: dict[str, Any], inboxes: tuple[ProtectedInbox, ...], primary: ProtectedInbox) -> dict[str, Any]:
    by_id = {inbox.inbox_id: inbox for inbox in inboxes}
    if primary.inbox_id not in by_id or by_id[primary.inbox_id] is not primary:
        raise MemoryFailure('protected_multi_binding_mismatch')
    entries = []
    for item in snapshot['inboxes']:
        inbox = by_id[item['inbox_id']]
        entries.append({'inbox_id': inbox.inbox_id, 'key_id': item['key_id'], 'inbox_dir': str(inbox.root),
                        'key_dir': str(inbox.key_root), 'material': item['material']})
    return {'version': 1, 'ledger_id': snapshot['ledger_id'], 'primary': primary.inbox_id, 'inboxes': entries}


def activate(runtime: MemoryRuntime, access: Access, primary: ProtectedInbox,
             inboxes: tuple[ProtectedInbox, ...]) -> None:
    """Persist binding then mode only while a fresh proof and all locks hold."""
    runtime._require_scope(access, 'admin', 'protected_multi_activate')
    def commit(snapshot: dict[str, Any]) -> None:
        _check(runtime, access, snapshot)
        config = _load(runtime.root / 'runtime.json')
        if config.get('service_mode') is True or config.get('protected_multi_binding') is True:
            raise MemoryFailure('invalid_service_mode')
        content = _content(snapshot, inboxes, primary)
        existing = read(runtime)
        if existing is not None and existing != content:
            raise MemoryFailure('protected_multi_binding_mismatch')
        _atomic_json(runtime.root / BINDING_NAME, {'content': content, 'authentication': _auth(runtime, content)})
        _atomic_json(runtime.root / 'runtime.json', {**config, 'service_mode': True, 'protected_multi_binding': True})
    _inspect(runtime, access, tuple((inbox, None) for inbox in inboxes), decrypt=False,
             require_complete=True, locked=commit)


def verify_startup(runtime: MemoryRuntime, primary: ProtectedInbox, inboxes: tuple[ProtectedInbox, ...]) -> None:
    """Check a persisted binding and all current objects without unlocking originals."""
    def check(snapshot: dict[str, Any]) -> None:
        content = read(runtime)
        if content is None or content != _content(snapshot, inboxes, primary):
            raise MemoryFailure('protected_multi_binding_mismatch')
        if any(item['pending'] for item in snapshot['inboxes'] if item['inbox_id'] != primary.inbox_id):
            raise MemoryFailure('protected_cutover_requires_migration')
    _inspect(runtime, None, tuple((inbox, None) for inbox in inboxes), decrypt=False, require_complete=False,
             require_readable=False, unbounded_objects=True, service_startup=True, locked=check)
