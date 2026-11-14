"""Authenticated reattachment of a complete staged multi-inbox set (#237).

A staged bundle is never trusted on its own: under the stopped-service lock,
the exclusive ownership gate and one ledger transaction this revalidates the
signed bundle and stage proof against the current recovered receipt, ledger
links and registry, materializes every vault into a registered secure
workspace, re-unlocks and re-decrypts every object, publishes the vault set
into explicit destinations and only then removes the missing-vault gate in
the same transaction that records the authenticated multi attachment.  A
crash before that commit leaves the gate intact and the retryable state on
disk; a committed attach is replay-resistant because the missing state is
gone.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, BinaryIO, Callable, cast
import zipfile

from .backup import _decrypt_stream
from .missing_vault import _receipt, attachment, multi_attachment, read_state
from .models import Access, MemoryFailure
from .paths import external, verify_private_directory
from .protected import ProtectedInbox
from .runtime import MemoryRuntime, _atomic_json, _json, _json as _json, _load, _unique_object
from .secure_ntfs import HeldFile, PrivateTree
from .vault_migration import _authorize_admin, _file_bytes
from .vault_multi_backup import _link_digest
from .vault_multi_binding import BINDING_NAME, _auth
from .vault_multi_verify import _inspect
from .vault_restore import _stopped_service
from .vault_stage_check import _locked_read, _verified_locked
from .vault_workspace import secure_workspace


@dataclass(frozen=True)
class MultiVaultAttachment:
    backup_id: str
    inboxes: int
    protected_objects: int
    destinations: list[str]
    daily_admitted: bool = False


_MEMBER_FILES = (('inbox', 'inbox', 'inbox.sqlite3', True),
                 ('receiving', 'keys', 'receiving.json', False),
                 ('writer', 'keys', 'writer.key', False),
                 ('recovery', 'keys', 'recovery.json', False))


def _material_digests(key_root: Path, fingerprint_key: bytes) -> list[str]:
    """Same digest form as vault_migration._materials for published key files."""
    verify_private_directory(key_root)
    return [hmac.digest(fingerprint_key, b'protected-cutover-material-v1\0'
                        + path.name.encode() + b'\0' + _file_bytes(path, 65536), hashlib.sha256).hex()
            for path in (key_root / 'receiving.json', key_root / 'writer.key', key_root / 'recovery.json')]


def _decrypted_size_ok(name: str, info: Any, encrypted: Any) -> bool:
    maximum = 1073741824 if name.endswith('-inbox') else 65536
    return (isinstance(info, dict) and type(info.get('plaintext_size')) is int
            and 0 <= info['plaintext_size'] <= maximum and encrypted.file_size <= maximum * 2 + 65536)


def _digest_of(stream: Any) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _copy_held(source: HeldFile, target: HeldFile) -> None:
    source.stream.seek(0)
    target.stream.seek(0)
    while True:
        chunk = source.stream.read(1024 * 1024)
        if not chunk:
            break
        target.stream.write(chunk)
    target.stream.flush()
    os.fsync(target.stream.fileno())


def _discard_empty_destination(destination_parent: Path, dest_tree: PrivateTree | None,
                               published_any: bool, existed_before: bool) -> None:
    """Remove a destination root this run created when nothing was published."""
    if dest_tree is None or published_any or existed_before:
        return
    try:
        destination_parent.rmdir()
    except OSError:
        pass


def attach_stage(config: Path, access: Access, bundle: Path, recovery_file: Path, backup_password: bytes,
                 password_for_inbox: Callable[[str, str], bytes], destination_parent: Path, *,
                 cancel: threading.Event | None = None, timeout: float = 120) -> MultiVaultAttachment:
    """Reverify and publish the whole staged set; the gate falls only at commit."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120 or not callable(password_for_inbox):
        raise MemoryFailure('invalid_vault_attach_request')
    config, bundle, recovery_file, destination_parent = map(external, (config, bundle, recovery_file, destination_parent))
    root = config.parent
    if any(destination_parent.is_relative_to(path) or path.is_relative_to(destination_parent)
           for path in (root, recovery_file.parent)):
        raise MemoryFailure('unsafe_backup_path')
    deadline = time.monotonic() + timeout

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('vault_attach_canceled')
        if time.monotonic() >= deadline:
            raise MemoryFailure('vault_attach_timeout')

    def remaining() -> float:
        check()
        return deadline - time.monotonic()

    check()
    dest_tree: PrivateTree | None = None
    published_any = False
    existed_before = False
    try:
        with _stopped_service(root):
            runtime = MemoryRuntime(config, service_owner=True)
            _authorize_admin(runtime, access)
            with runtime._connection(ownership_exclusive=True, check=remaining) as core:
                core.execute('BEGIN IMMEDIATE')
                _authorize_admin(runtime, access)
                # The gate is still up and no attachment exists; a committed
                # attach can never be replayed because this refuses.
                if not read_state(runtime, core):
                    raise MemoryFailure('protected_recovery_proof_invalid')
                if attachment(runtime, core) is not None or multi_attachment(runtime, core) is not None:
                    raise MemoryFailure('protected_recovery_proof_invalid')
                initial_config = _load(config)
                existing_binding = None
                if initial_config.get('protected_multi_binding') is True:
                    from .vault_multi_binding import read as read_binding
                    existing_binding = read_binding(runtime)
                with _locked_read(bundle) as stream:
                    with _verified_locked(runtime, access, core, stream, recovery_file, backup_password,
                                          check=check, deadline=deadline, cancel=cancel) as verified:
                        members = verified.members
                        proof_binding = verified.proof['binding']
                        private = verified.private
                        backup_id = verified.proof['backup_id']
                        signed_files = verified.signed['files']
                        archive = verified.archive

                        def decrypt_member(name: str, output: Any) -> None:
                            check()
                            info = signed_files[name]
                            if not _decrypted_size_ok(name, info, archive.getinfo(name + '.enc')):
                                raise MemoryFailure('invalid_backup')
                            aad = _json({'backup_id': backup_id, 'backup_key_id': verified.signed['backup_key_id'],
                                         'file': name}).encode()
                            with archive.open(name + '.enc') as encrypted:
                                _decrypt_stream(cast(BinaryIO, encrypted), output, private, info, aad, check=check)

                        destinations: dict[str, tuple[Path, Path]] = {}
                        workspace_materials: dict[str, list[str]] = {}
                        existed_before = destination_parent.exists()
                        dest_tree = PrivateTree(destination_parent if existed_before else destination_parent.parent)
                        with dest_tree as dest_anchor:
                            dest_root = dest_anchor if existed_before else dest_anchor.mkdir(destination_parent.name)
                            with secure_workspace(runtime, destination_parent, 'multi', core=core) as temporary:
                                vaults = temporary.mkdir('vaults')
                                clone_dir = temporary.mkdir('ledger')
                                clone = clone_dir.path
                                held: list[HeldFile] = []
                                try:
                                    slot_paths: dict[str, tuple[Path, Path]] = {}
                                    sources: dict[str, dict[str, HeldFile]] = {}
                                    for member in members:
                                        slot = member['slot']
                                        slot_dir = vaults.mkdir(slot)
                                        inbox_dir, key_dir = slot_dir.mkdir('inbox'), slot_dir.mkdir('keys')
                                        slot_paths[slot] = (inbox_dir.path, key_dir.path)
                                        sources[slot] = {}
                                        for name, subdir, filename, writable in _MEMBER_FILES:
                                            holder = inbox_dir if subdir == 'inbox' else key_dir
                                            source = holder.create_file(filename, write_share=writable)
                                            held.append(source)
                                            sources[slot][filename] = source
                                            decrypt_member(f'{slot}-{name}', source.stream)
                                            source.stream.flush()
                                            os.fsync(source.stream.fileno())
                                    ledger_file = clone_dir.create_file('memory.sqlite3', write_share=True)
                                    held.append(ledger_file)
                                    with closing(sqlite3.connect((root / 'memory.sqlite3').as_uri() + '?mode=ro',
                                                                 uri=True)) as source_db:
                                        with closing(sqlite3.connect(clone / 'memory.sqlite3')) as copied:
                                            source_db.backup(copied, pages=256, progress=lambda *_: check(), sleep=.05)
                                            copied.execute('DROP TABLE IF EXISTS restored_missing_vault_v1')
                                            copied.execute('DROP TABLE IF EXISTS restored_vault_attachment_v1')
                                            copied.execute('DROP TABLE IF EXISTS restored_multi_attachment_v1')
                                            copied.commit()
                                    for filename, payload in (
                                            ('runtime.json', _json({'schema_version': 1, 'data_dir': str(clone),
                                                                            'busy_timeout_ms': 5000}).encode()),
                                            ('client.json', (root / 'client.json').read_bytes()),
                                            ('idempotency.key', runtime._fingerprint_key)):
                                        side = clone_dir.create_file(filename)
                                        held.append(side)
                                        side.stream.write(payload)
                                        side.stream.flush()
                                        os.fsync(side.stream.fileno())
                                    isolated = MemoryRuntime(clone / 'runtime.json')
                                    declared = []
                                    for member in members:
                                        inbox_root, key_root = slot_paths[member['slot']]
                                        inbox_file = inbox_root / 'inbox.sqlite3'
                                        with closing(sqlite3.connect(inbox_file.as_uri() + '?mode=ro', uri=True)) as integrity:
                                            integrity.set_progress_handler(lambda: int(time.monotonic() >= deadline
                                                or (cancel is not None and cancel.is_set())), 1000)
                                            try:
                                                if integrity.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                                                    raise MemoryFailure('vault_restore_integrity_failed')
                                            except sqlite3.OperationalError:
                                                check()
                                                raise
                                        inbox = ProtectedInbox(isolated, inbox_root, key_root)
                                        if (inbox.inbox_id != member['inbox_id']
                                                or str(inbox.public['key_id']) != member['key_id']):
                                            raise MemoryFailure('vault_backup_ledger_mismatch')
                                        declared.append((inbox, password_for_inbox(member['slot'], member['inbox_id'])))

                                    def verify(snapshot: dict[str, Any], copied_core: sqlite3.Connection,
                                               stores: list[Any]) -> None:
                                        if snapshot['ledger_id'] != proof_binding['ledger_id']:
                                            raise MemoryFailure('vault_backup_ledger_mismatch')
                                        for inbox, _private, store, _material in stores:
                                            member = next(item for item in members if item['inbox_id'] == inbox.inbox_id)
                                            summary = next(item for item in snapshot['inboxes']
                                                           if item['inbox_id'] == inbox.inbox_id)
                                            if (summary['objects'] != member['objects'] or not hmac.compare_digest(
                                                    _link_digest(copied_core, store, check), member['link_digest'])):
                                                raise MemoryFailure('vault_backup_ledger_mismatch')
                                            digest = hashlib.sha256()
                                            for row in store.execute('SELECT id,source FROM objects ORDER BY id'):
                                                check()
                                                saved = copied_core.execute(
                                                    'SELECT event FROM messages WHERE source=?', (row['source'],)).fetchone()
                                                if saved is None:
                                                    raise MemoryFailure('vault_backup_ledger_mismatch')
                                                digest.update((_json([row['id'], saved['event']]) + '\n').encode())
                                            if not hmac.compare_digest(digest.hexdigest(),
                                                                       verified.proof['projection_digests'][inbox.inbox_id]):
                                                raise MemoryFailure('vault_backup_ledger_mismatch')
                                            workspace_materials[inbox.inbox_id] = list(summary['material'])

                                    _inspect(isolated, access, tuple(declared), decrypt=True, require_complete=True,
                                             require_readable=False, locked_stores=verify, deadline_limit=deadline,
                                             check_extra=check, cancel=cancel)
                                    # Ensure every decrypted inbox is fully inside its
                                    # held main file before it is copied anywhere.
                                    for member in members:
                                        inbox_root, _key_root = slot_paths[member['slot']]
                                        with closing(sqlite3.connect(str(inbox_root / 'inbox.sqlite3'),
                                                                     isolation_level=None)) as checkpoint:
                                            outcome = checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                                            if int(outcome[0]) != 0:
                                                raise MemoryFailure('vault_restore_integrity_failed')
                                    for source in held:
                                        source.verify()

                                    # Publish every slot into explicit destinations while
                                    # every source hold is still open and verified.
                                    for member in members:
                                        slot = member['slot']
                                        try:
                                            slot_root = dest_root.mkdir(slot)
                                        except MemoryFailure:
                                            slot_root = dest_root.hold_dir(slot)
                                        try:
                                            dest_inbox = slot_root.mkdir('inbox')
                                        except MemoryFailure:
                                            dest_inbox = slot_root.hold_dir('inbox')
                                        try:
                                            dest_keys = slot_root.mkdir('keys')
                                        except MemoryFailure:
                                            dest_keys = slot_root.hold_dir('keys')
                                        destinations[slot] = (dest_inbox.path, dest_keys.path)
                                        for name, subdir, filename, writable in _MEMBER_FILES:
                                            source = sources[slot][filename]
                                            holder = dest_inbox if subdir == 'inbox' else dest_keys
                                            expected = _digest_of(source.stream)
                                            try:
                                                target = holder.create_file(filename, write_share=writable)
                                                _copy_held(source, target)
                                            except MemoryFailure:
                                                target = holder.open_file(filename, write_share=writable)
                                                if not hmac.compare_digest(_digest_of(target.stream), expected):
                                                    raise MemoryFailure('vault_attach_destination_mismatch') from None
                                            if writable and subdir == 'inbox':
                                                for journal in ('inbox.sqlite3-wal', 'inbox.sqlite3-shm'):
                                                    try:
                                                        holder.create_file(journal, write_share=True).release()
                                                    except MemoryFailure:
                                                        pass
                                        published = _material_digests(dest_keys.path, runtime._fingerprint_key)
                                        if not hmac.compare_digest(_json(published).encode(),
                                                                   _json(workspace_materials[member['inbox_id']]).encode()):
                                            raise MemoryFailure('vault_attach_destination_mismatch')
                                        published_any = True
                                finally:
                                    for source in held:
                                        source.release()

                                # Final verification at the published locations,
                                # directly against the current recovered ledger.
                                for member in members:
                                    slot = member['slot']
                                    final_inbox, final_keys = destinations[slot]
                                    verify_private_directory(final_inbox)
                                    verify_private_directory(final_keys)
                                    with closing(sqlite3.connect(str(final_inbox / 'inbox.sqlite3'), timeout=1,
                                                                 isolation_level=None)) as db:
                                        db.row_factory = sqlite3.Row
                                        try:
                                            if [row[0] for row in db.execute('PRAGMA quick_check').fetchall()] != ['ok']:
                                                raise MemoryFailure('vault_restore_integrity_failed')
                                            settings = db.execute('SELECT * FROM settings WHERE singleton=1').fetchone()
                                            if settings is None:
                                                raise MemoryFailure('protected_binding_mismatch')
                                            public = json.loads(settings['public'], object_pairs_hook=_unique_object)
                                            binding_key = hmac.digest(runtime._fingerprint_key,
                                                                      b'evernook-protected-inbox-v1', hashlib.sha256)
                                            source_inbox = next(item for item in declared
                                                                if item[0].inbox_id == member['inbox_id'])[0]
                                            if (settings['inbox_id'] != member['inbox_id']
                                                    or str(public.get('key_id')) != member['key_id']
                                                    or public != source_inbox.public
                                                    or not hmac.compare_digest(bytes(settings['runtime_binding']), binding_key)):
                                                raise MemoryFailure('protected_binding_mismatch')
                                            objects = int(db.execute('SELECT count(*) FROM objects').fetchone()[0])
                                            if objects != member['objects']:
                                                raise MemoryFailure('vault_backup_ledger_mismatch')
                                            links = int(core.execute(
                                                "SELECT count(*) FROM protected_links_v1 l JOIN messages m ON m.id=l.message_id "
                                                "WHERE json_extract(l.reference,'$.inbox_id')=?",
                                                (member['inbox_id'],)).fetchone()[0])
                                            if links != member['objects']:
                                                raise MemoryFailure('vault_backup_ledger_mismatch')
                                            if not hmac.compare_digest(_link_digest(core, db, check), member['link_digest']):
                                                raise MemoryFailure('vault_backup_ledger_mismatch')
                                        except sqlite3.OperationalError:
                                            check()
                                            raise

                            binding_entries = [{'inbox_id': member['inbox_id'], 'key_id': member['key_id'],
                                                'inbox_dir': str(destinations[member['slot']][0]),
                                                'key_dir': str(destinations[member['slot']][1]),
                                                'material': workspace_materials[member['inbox_id']]}
                                               for member in sorted(members, key=lambda item: item['inbox_id'])]
                        binding_content = {'version': 1, 'ledger_id': proof_binding['ledger_id'],
                                           'primary': proof_binding['primary'], 'inboxes': binding_entries}
                        if existing_binding is not None and existing_binding != binding_content:
                            raise MemoryFailure('protected_multi_binding_mismatch')
                        _authorize_admin(runtime, access)
                        check()
                        _atomic_json(runtime.root / BINDING_NAME,
                                     {'content': binding_content, 'authentication': _auth(runtime, binding_content)})
                        _atomic_json(config, {**_load(config), 'protected_multi_binding': True})
                        content = {'version': 1, 'receipt_auth': _receipt(runtime), 'binding': binding_content}
                        authentication = hmac.digest(runtime._fingerprint_key, b'evernook-multi-vault-attachment-v1\0'
                                                     + _json(content).encode(), hashlib.sha256).hex()
                        core.execute('CREATE TABLE restored_multi_attachment_v1 '
                                     '(id INTEGER PRIMARY KEY CHECK(id=1), content TEXT NOT NULL, authentication TEXT NOT NULL)')
                        core.execute('INSERT INTO restored_multi_attachment_v1 VALUES (1,?,?)', (_json(content), authentication))
                        core.execute('DROP TABLE restored_missing_vault_v1')
                        core.execute('COMMIT')
                        return MultiVaultAttachment(backup_id, len(members), sum(item['objects'] for item in members),
                                                    [str(destination_parent / member['slot']) for member in members])
    except MemoryFailure:
        _discard_empty_destination(destination_parent, dest_tree, published_any, existed_before)
        raise
    except sqlite3.Error:
        check()
        _discard_empty_destination(destination_parent, dest_tree, published_any, existed_before)
        raise MemoryFailure('vault_attach_failed') from None
    except (OSError, ValueError, TypeError, KeyError, StopIteration, zipfile.BadZipFile):
        _discard_empty_destination(destination_parent, dest_tree, published_any, existed_before)
        raise MemoryFailure('vault_attach_failed') from None
