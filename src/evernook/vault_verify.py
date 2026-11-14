"""Trusted local ciphertext verification for an explicitly chosen inbox."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from .models import Access, MemoryFailure
from .protected import ProtectedInbox
from .vault_crypto import _decrypt_event, _unlock


def verify_page(inbox: ProtectedInbox, access: Access, password: bytes, *,
                after: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Check one inbox page without returning a decrypted event or authorizing migration."""
    inbox.runtime._require_scope(access, 'admin', 'protected_verify')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise MemoryFailure('invalid_page')
    if after is not None:
        try:
            if str(UUID(after)) != after:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MemoryFailure('invalid_protected_id') from None
    private = _unlock(inbox, password)
    inbox.runtime._require_scope(access, 'admin', 'protected_verify')
    with inbox._connection() as db:
        db.execute('BEGIN')
        rows = db.execute('SELECT * FROM objects WHERE (? IS NULL OR id>?) ORDER BY id LIMIT ?',
                          (after, after, limit + 1)).fetchall()
        objects = []
        for row in rows[:limit]:
            receipt = inbox._receipt(row)
            _decrypt_event(inbox, row, private)
            objects.append({'object_id': receipt.object_id, 'state': receipt.state})
        cursor = str(rows[limit - 1]['id']) if len(rows) > limit else None
    inbox.runtime._require_scope(access, 'admin', 'protected_verify')
    return {'inbox_id': inbox.inbox_id, 'key_id': str(inbox.public['key_id']), 'objects': objects,
            'next_cursor': cursor, 'scope': 'current_page_only', 'migration_authorized': False}
