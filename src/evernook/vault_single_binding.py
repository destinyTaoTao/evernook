"""Authenticated service binding for one declared protected inbox (#241).

A normally activated single-inbox service previously persisted no durable
binding: the protected roots only existed as startup arguments, so cleanup
and destruction could not distinguish an active original from an unregistered
crash copy.  This module records an explicit, HMAC-authenticated binding at
activation and lets guarded flows verify it afterwards.  Restored single
attachments keep their authenticated ledger marker; both states are accepted
by the active-path guard.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .models import MemoryFailure
from .paths import verify_private_directory
from .runtime import MemoryRuntime, _atomic_json, _json, _unique_object
from .vault_migration import _file_bytes


BINDING_NAME = 'protected-single-binding.json'


def _auth(runtime: MemoryRuntime, content: dict[str, Any]) -> str:
    return hmac.digest(runtime._fingerprint_key, b'protected-single-binding-v1\0'
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
        if (set(content) != {'version', 'inbox_id', 'key_id', 'inbox_dir', 'key_dir', 'material'}
                or type(content['version']) is not int or content['version'] != 1
                or not isinstance(content['inbox_id'], str) or not isinstance(content['key_id'], str)
                or not isinstance(content['inbox_dir'], str) or not isinstance(content['key_dir'], str)
                or not isinstance(content['material'], list) or len(content['material']) != 3
                or any(not isinstance(item, str) for item in content['material'])
                or not isinstance(authentication, str) or len(authentication) != 64
                or not hmac.compare_digest(authentication, _auth(runtime, content))):
            raise ValueError()
        return content
    except (ValueError, TypeError, KeyError, UnicodeError, MemoryFailure):
        raise MemoryFailure('protected_single_binding_invalid') from None


def write(runtime: MemoryRuntime, content: dict[str, Any]) -> None:
    _atomic_json(runtime.root / BINDING_NAME, {'content': content, 'authentication': _auth(runtime, content)})
