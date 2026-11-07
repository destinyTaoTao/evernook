"""Trusted host recovery decisions, never model/MCP operations. Stores IDs and keyed fingerprints only."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from typing import Any
from uuid import uuid4

from .models import Access, MemoryFailure
from .runtime import MemoryRuntime, _identifier, _json


class HostRecovery:
    def __init__(self, runtime: MemoryRuntime):
        self.runtime = runtime

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS host_recovery_previews_v1 (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            target TEXT NOT NULL, refs TEXT NOT NULL, expires REAL NOT NULL,
            canceled INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS host_recovery_operations_v1 (
            target TEXT PRIMARY KEY, client_id TEXT NOT NULL, state TEXT NOT NULL, refs TEXT NOT NULL)""")

    def _references(self, db: sqlite3.Connection, refs: Any) -> None:
        if not isinstance(refs, list) or len(refs) > 64:
            raise MemoryFailure('invalid_recovery_review')
        seen = set()
        for ref in refs:
            if (not isinstance(ref, list) or len(ref) != 4 or not isinstance(ref[0], str)
                    or type(ref[1]) is not int or ref[1] < 1 or type(ref[2]) is not int or ref[2] < 1
                    or type(ref[3]) is not bool or ref[0] in seen):
                raise MemoryFailure('invalid_recovery_review')
            _identifier(ref[0])
            seen.add(ref[0])
            state = self.runtime._state(db, ref[0])
            row = db.execute('SELECT event,receipt FROM messages WHERE id=?', (ref[0],)).fetchone()
            manifest = db.execute('SELECT manifest FROM captures WHERE message_id=?', (ref[0],)).fetchone()
            if (state.status != 'active' or state.version != ref[1] or row is None or manifest is None):
                raise MemoryFailure('recovery_stale')
            stored = self.runtime._stored(row)
            capture = self.runtime._check_capture(db, self.runtime._capture_receipt(manifest['manifest']))
            if (not stored.receipt.original_saved or stored.receipt.content_version != ref[2]
                    or capture.complete != ref[3]):
                raise MemoryFailure('recovery_stale')

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {'operation_id': row['target'], 'target_session': row['target'], 'state': row['state'], 'read_only': True}

    def run(self, access: Access, payload: dict[str, Any]) -> dict[str, Any]:
        identity = self.runtime._require_scope(access, 'maintain', 'host_recovery')
        if not {'recall', 'accept'}.issubset(identity.scopes):
            raise MemoryFailure('permission_denied')
        action = payload.get('action')
        keys = {'preview': {'action', 'fingerprint', 'references'}, 'confirm': {'action', 'proposal_id', 'fingerprint'},
                'cancel': {'action', 'proposal_id'}, 'complete': {'action', 'operation_id'}}
        if not isinstance(action, str) or action not in keys or set(payload) != keys[action]:
            raise MemoryFailure('invalid_recovery_action')
        with self.runtime._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            if action == 'preview':
                fingerprint = payload['fingerprint']
                if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
                    raise MemoryFailure('invalid_recovery_review')
                self._references(db, payload['references'])
                now = time.time()
                db.execute('DELETE FROM host_recovery_previews_v1 WHERE confirmed=0 AND expires<?', (now,))
                count = db.execute('SELECT count(*) FROM host_recovery_previews_v1 WHERE client_id=? AND confirmed=0 AND canceled=0',
                                   (identity.client_id,)).fetchone()[0]
                if count >= 64:
                    raise MemoryFailure('recovery_queue_full')
                key = hmac.digest(self.runtime._fingerprint_key, _json([identity.client_id, fingerprint, payload['references']]).encode(), hashlib.sha256).hex()
                target = 'evernook-recovery-' + key[:40]
                proposal_id = str(uuid4())
                db.execute('INSERT INTO host_recovery_previews_v1 VALUES (?,?,?,?,?,?,0,0)',
                           (proposal_id, identity.client_id, fingerprint, target, _json(payload['references']), now + 300))
                result = {'proposal_id': proposal_id, 'target_session': target, 'expires_at': now + 300, 'read_only': True}
            elif action == 'complete':
                _identifier(payload['operation_id'])
                row = db.execute('SELECT * FROM host_recovery_operations_v1 WHERE target=? AND client_id=?',
                                 (payload['operation_id'], identity.client_id)).fetchone()
                if row is None:
                    raise MemoryFailure('recovery_not_found')
                if row['state'] != 'complete':
                    self._references(db, json.loads(row['refs']))
                    db.execute("UPDATE host_recovery_operations_v1 SET state='complete' WHERE target=?", (row['target'],))
                result = {**self._receipt(row), 'state': 'complete'}
            else:
                _identifier(payload['proposal_id'])
                row = db.execute('SELECT * FROM host_recovery_previews_v1 WHERE id=? AND client_id=?',
                                 (payload['proposal_id'], identity.client_id)).fetchone()
                if row is None:
                    raise MemoryFailure('recovery_not_found')
                operation = db.execute('SELECT * FROM host_recovery_operations_v1 WHERE target=?', (row['target'],)).fetchone()
                if row['confirmed'] and operation is None:
                    raise MemoryFailure('recovery_unavailable')
                if action == 'cancel':
                    if not row['confirmed']:
                        db.execute('UPDATE host_recovery_previews_v1 SET canceled=1 WHERE id=?', (row['id'],))
                    result = self._receipt(operation) if row['confirmed'] else {'canceled': True}
                else:
                    if payload['fingerprint'] != row['fingerprint']:
                        raise MemoryFailure('recovery_review_mismatch')
                    if row['canceled']:
                        raise MemoryFailure('recovery_canceled')
                    if not row['confirmed'] and row['expires'] <= time.time():
                        raise MemoryFailure('recovery_expired')
                    if operation is None or operation['state'] != 'complete':
                        self._references(db, json.loads(row['refs']))
                    if operation is None:
                        db.execute("INSERT INTO host_recovery_operations_v1 VALUES (?,?,'confirmed',?)",
                                   (row['target'], identity.client_id, row['refs']))
                    db.execute('UPDATE host_recovery_previews_v1 SET confirmed=1 WHERE id=?', (row['id'],))
                    operation = db.execute('SELECT * FROM host_recovery_operations_v1 WHERE target=?', (row['target'],)).fetchone()
                    result = self._receipt(operation)
            db.execute('COMMIT')
            return result
