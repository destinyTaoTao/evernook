"""Short-lived authenticated multi-inbox verification; never activates service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any

from .models import Access, MemoryFailure
from .paths import verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_json, _json, _unique_object
from .vault_migration import _authorize_admin, _file_bytes
from .vault_multi_verify import _inspect


PROOF_NAME = 'protected-multi-cutover-proof.json'


def _authentication(runtime: MemoryRuntime, proof: dict[str, Any]) -> str:
    return hmac.digest(runtime._fingerprint_key, b'protected-multi-cutover-proof-v1\0'
                       + _json(proof).encode(), hashlib.sha256).hex()


def invalidate(runtime: MemoryRuntime, access: Access) -> None:
    runtime._require_scope(access, 'admin', 'protected_multi_prepare')
    with runtime._connection(ownership_exclusive=True) as core:
        core.execute('BEGIN IMMEDIATE')
        _authorize_admin(runtime, access)
        path = runtime.root / PROOF_NAME
        verify_private_directory(runtime.root, extra_files=(path,))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise MemoryFailure('protected_migration_proof_unavailable') from None
        core.execute('COMMIT')


def prepare(runtime: MemoryRuntime, access: Access, declared: tuple[tuple[ProtectedInbox, bytes], ...], *,
            inventory_statement: str) -> dict[str, Any]:
    """Create a proof only after all declared originals decrypt under the same locks."""
    invalidate(runtime, access)
    if inventory_statement != 'STOPPED_AND_INVENTORIED':
        raise MemoryFailure('protected_migration_inventory_unconfirmed')
    saved: dict[str, Any] = {}
    def save(snapshot: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        proof: dict[str, Any] = {'version': 1, 'owner': access.client_id, 'issued_at': now.isoformat(),
                                 'expires_at': (now + timedelta(minutes=10)).isoformat(), 'snapshot': snapshot}
        proof['authentication'] = _authentication(runtime, proof)
        _atomic_json(runtime.root / PROOF_NAME, proof)
        saved.update({'expires_at': proof['expires_at'], 'verified_objects': snapshot['objects'],
                      'proof_saved': True, 'migration_completed': False})
    _inspect(runtime, access, declared, decrypt=True, require_complete=True, locked=save)
    return saved


def _check(runtime: MemoryRuntime, access: Access, snapshot: dict[str, Any]) -> None:
    path = runtime.root / PROOF_NAME
    verify_private_directory(runtime.root, extra_files=(path,))
    try:
        proof: object = json.loads(_file_bytes(path, 65536).decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
    except (MemoryFailure, ValueError, UnicodeError):
        raise MemoryFailure('protected_migration_proof_invalid') from None
    if not isinstance(proof, dict):
        raise MemoryFailure('protected_migration_proof_invalid')
    authentication = proof.pop('authentication', None)
    if (set(proof) != {'version', 'owner', 'issued_at', 'expires_at', 'snapshot'}
            or type(proof['version']) is not int or proof['version'] != 1 or proof['owner'] != access.client_id
            or not isinstance(authentication, str) or len(authentication) != 64
            or any(char not in '0123456789abcdef' for char in authentication)
            or not hmac.compare_digest(authentication, _authentication(runtime, proof))):
        raise MemoryFailure('protected_migration_proof_invalid')
    try:
        issued, expires = (datetime.fromisoformat(proof[key]) for key in ('issued_at', 'expires_at'))
        now = datetime.now(timezone.utc)
        if (issued.tzinfo is None or expires.tzinfo is None or not issued <= now <= expires
                or expires - issued > timedelta(minutes=10)):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise MemoryFailure('protected_migration_proof_invalid') from None
    if proof['snapshot'] != snapshot:
        raise MemoryFailure('protected_migration_proof_stale')
    _authorize_admin(runtime, access)
    if not issued <= datetime.now(timezone.utc) <= expires:
        raise MemoryFailure('protected_migration_proof_invalid')


def revalidate(runtime: MemoryRuntime, access: Access, inboxes: tuple[ProtectedInbox, ...]) -> dict[str, Any]:
    """Advisory proof check; a later activation must repeat it under its own locks."""
    runtime._require_scope(access, 'admin', 'protected_multi_revalidate')
    declared = tuple((inbox, None) for inbox in inboxes)
    result = _inspect(runtime, access, declared, decrypt=False, require_complete=True,
                      locked=lambda snapshot: _check(runtime, access, snapshot))
    return {'proof_valid': True, 'objects': result['objects'], 'migration_authorized': False}
