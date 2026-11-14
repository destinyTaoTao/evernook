"""Trusted local review and explicit publication of an ordinary encrypted backup."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4

from nacl.exceptions import BadSignatureError, CryptoError

from .archive import _database, verify_archive
from .backup import _digest, _unpack_backup
from .models import Access, MemoryFailure
from .paths import external, provision_private_directory, verify_private_directory
from .runtime import MemoryRuntime, _access_record, _atomic_bytes, _atomic_json, _identities, _json, _load


ACKNOWLEDGEMENT = 'OPEN_SNAPSHOT_WITH_UNKNOWN_LATER_CHANGES'


def _package_fingerprint(root: Path) -> str:
    names = ('backup.json', 'manifest.enc', 'evidence.enc', 'runtime.enc')
    verify_private_directory(root, extra_files=tuple(root / name for name in names))
    if {item.name for item in root.iterdir()} != set(names):
        raise MemoryFailure('invalid_backup')
    return hashlib.sha256(_json([(name, _digest(root / name)) for name in names]).encode()).hexdigest()


def _receipt_auth(key: bytes, content: dict[str, Any]) -> str:
    return hmac.digest(key, b'evernook-offline-recovery-v1\0' + _json(content).encode(), hashlib.sha256).hex()


class OfflineRecovery:
    """Trusted local caller must establish genuine user confirmation before publishing."""

    def __init__(self, source: Path, destination: Path, recovery_file: Path, password: bytes):
        self.source, self.destination, recovery_file = external(source), external(destination), external(recovery_file)
        if (self.source.is_relative_to(self.destination) or self.destination.is_relative_to(self.source)
                or recovery_file.is_relative_to(self.destination) or self.destination.is_relative_to(recovery_file.parent)):
            raise MemoryFailure('unsafe_backup_path')
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix='.evernook-offline-recovery-', dir=self.destination.parent)
            self._archive = Path(self._temporary.name) / 'archive'
            before = _package_fingerprint(self.source)
            self._status, self._key = _unpack_backup(self.source, self._archive, recovery_file, password)
            if before != _package_fingerprint(self.source):
                raise MemoryFailure('backup_changed_during_review')
            if _load(self._archive / 'manifest.json').get('version') != 2:
                raise MemoryFailure('backup_runtime_provenance_required')
            self._fingerprint = before
            self._archive_sha = verify_archive(self._archive).sha256
            self._manifest_sha = _digest(self._archive / 'manifest.json')
            with _database(self._archive / 'evidence.sqlite3') as db:
                ledger = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_identity_v1'").fetchone()
                if not ledger:
                    raise MemoryFailure('backup_runtime_provenance_required')
                registry = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_inbox_registry_v1'").fetchone()
                links = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_links_v1'").fetchone()
                if links:
                    orphan = db.execute("""SELECT 1 FROM messages m LEFT JOIN protected_links_v1 l ON l.message_id=m.id
                        WHERE json_extract(m.event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' AND l.message_id IS NULL LIMIT 1""").fetchone()
                else:
                    orphan = db.execute("SELECT 1 FROM messages WHERE json_extract(event,'$.text')='[PROTECTED:ENCRYPTED_ORIGINAL]' LIMIT 1").fetchone()
                self._orphan = bool(orphan)
                self._protected = bool((registry and db.execute('SELECT 1 FROM protected_inbox_registry_v1 LIMIT 1').fetchone())
                                       or (links and db.execute('SELECT 1 FROM protected_links_v1 LIMIT 1').fetchone()) or orphan)
        except (CryptoError, BadSignatureError):
            self.close()
            raise MemoryFailure('backup_authentication_failed') from None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self.close()
            raise MemoryFailure('invalid_backup') from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self) -> OfflineRecovery:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def review(self) -> dict[str, Any]:
        if self._temporary is None:
            raise MemoryFailure('backup_review_closed')
        status = verify_archive(self._archive)
        return {'backup_id': self._status.backup_id, 'created_at': self._status.created_at,
                'messages': status.messages, 'important_items': status.important_items,
                'attachments': status.attachments, 'later_changes': 'unknown',
                'protected_references': self._protected, 'protected_orphan': self._orphan, 'vault_restored': False,
                'maintenance_at_recovery': 'paused', 'automatic_preference_admission_at_recovery': 'reset'}

    def publish(self, acknowledgement: str) -> dict[str, Any]:
        if self._temporary is None:
            raise MemoryFailure('backup_review_closed')
        if acknowledgement != ACKNOWLEDGEMENT:
            raise MemoryFailure('backup_confirmation_required')
        if self._orphan:
            raise MemoryFailure('backup_protected_orphan_requires_review')
        try:
            if (self._fingerprint != _package_fingerprint(self.source)
                    or _digest(self._archive / 'manifest.json') != self._manifest_sha
                    or verify_archive(self._archive).sha256 != self._archive_sha):
                raise MemoryFailure('backup_changed_during_review')
            request: dict[str, Any] = {'version': 1, 'backup_id': self._status.backup_id,
                'created_at': self._status.created_at, 'backup_fingerprint': self._fingerprint,
                'archive_sha256': self._archive_sha, 'destination': str(self.destination),
                'later_changes': 'unknown', 'protected_references': self._protected, 'vault_restored': False,
                'maintenance_at_recovery': 'paused', 'automatic_preference_admission_at_recovery': 'reset'}
            if self.destination.exists():
                return self._existing(request)
            staging = Path(self._temporary.name) / 'runtime'
            provision_private_directory(staging)
            shutil.copyfile(self._archive / 'evidence.sqlite3', staging / 'memory.sqlite3')
            if _digest(staging / 'memory.sqlite3') != self._archive_sha:
                raise MemoryFailure('backup_changed_during_review')
            with (staging / 'memory.sqlite3').open('rb+') as database:
                os.fsync(database.fileno())
            client_id, entry = 'recovered-' + uuid4().hex, 'recovered-' + uuid4().hex
            access = Access(client_id, secrets.token_urlsafe(32))
            _atomic_json(staging / 'runtime.json', {'schema_version': 1, 'data_dir': str(staging), 'busy_timeout_ms': 5000})
            _atomic_json(staging / 'client.json', {'schema_version': 2, 'client_id': client_id,
                'entry': entry, 'token': access.token, 'archive_review': True})
            _atomic_bytes(staging / 'idempotency.key', self._key)
            _atomic_json(staging / 'mcp-access-owner.json', asdict(access))
            runtime = MemoryRuntime(staging / 'runtime.json')
            runtime._authorize(access)
            from .scheduler import ConsolidationScheduler
            ConsolidationScheduler(runtime).pause(access, paused=True)
            with runtime._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preference_updates_settings_v1'").fetchone():
                    db.execute('UPDATE preference_updates_settings_v1 SET admitted_processor=NULL')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='consolidation_settings_v1'").fetchone():
                    db.execute("UPDATE consolidation_settings_v1 SET tier='review' WHERE tier='auto'")
                db.execute('COMMIT')
            _atomic_json(staging / 'runtime.json', {'schema_version': 1, 'data_dir': str(self.destination), 'busy_timeout_ms': 5000})
            content = {**request, 'owner_id': client_id, 'access_sha256': _digest(staging / 'mcp-access-owner.json')}
            _atomic_json(staging / 'recovery-receipt.json', {**content, 'authentication': _receipt_auth(self._key, content)})
            if self._fingerprint != _package_fingerprint(self.source):
                raise MemoryFailure('backup_changed_during_review')
            os.rename(staging, self.destination)
            return {'config': str(self.destination / 'runtime.json'), 'access_file': str(self.destination / 'mcp-access-owner.json'),
                    'backup_id': self._status.backup_id, 'later_changes': 'unknown',
                    'protected_references': self._protected, 'vault_restored': False,
                    'maintenance_at_recovery': 'paused', 'automatic_preference_admission_at_recovery': 'reset'}
        except (OSError, sqlite3.Error):
            raise MemoryFailure('backup_runtime_publish_failed') from None

    def _existing(self, request: dict[str, Any]) -> dict[str, Any]:
        root = self.destination
        verify_private_directory(root, extra_files=tuple(root / name for name in
            ('recovery-receipt.json', 'client.json', 'mcp-access-owner.json', 'idempotency.key', 'runtime.json')))
        receipt = _load(root / 'recovery-receipt.json')
        authentication = receipt.pop('authentication', None)
        access = _access_record(_load(root / 'mcp-access-owner.json'))
        content = {**request, 'owner_id': access.client_id, 'access_sha256': _digest(root / 'mcp-access-owner.json')}
        if (receipt != content or not isinstance(authentication, str)
                or not hmac.compare_digest(authentication, _receipt_auth(self._key, content))
                or not hmac.compare_digest((root / 'idempotency.key').read_bytes(), self._key)):
            raise MemoryFailure('backup_destination_exists')
        config = _load(root / 'runtime.json')
        if config.get('data_dir') != str(root) or not (root / 'memory.sqlite3').is_file():
            raise MemoryFailure('backup_destination_exists')
        identity = _identities(_load(root / 'client.json')).get(access.client_id)
        if identity is None or not hmac.compare_digest(identity.token, access.token):
            raise MemoryFailure('backup_destination_exists')
        return {'config': str(root / 'runtime.json'), 'access_file': str(root / 'mcp-access-owner.json'),
                'backup_id': self._status.backup_id, 'later_changes': 'unknown',
                'protected_references': self._protected, 'vault_restored': False,
                'maintenance_at_recovery': 'paused', 'automatic_preference_admission_at_recovery': 'reset'}
