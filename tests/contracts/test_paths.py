from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from evernook import MemoryFailure, MemoryRuntime, initialize
from evernook.paths import external, provision_private_directory, verify_private_directory
from evernook.windows_acl import _current_user_only, verify_dacl


@contextmanager
def resolved_as(original: Path, result: Path) -> Iterator[None]:
    actual = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        return result if path == original else actual(path, strict=strict)

    with patch.object(Path, 'resolve', new=resolve):
        yield


class PathTests(unittest.TestCase):
    def test_extended_local_dos_result_has_the_same_ordinary_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            ordinary = Path(directory).resolve() / 'memory.sqlite3-shm'
            with resolved_as(ordinary, Path('\\\\?\\' + str(ordinary))):
                self.assertEqual(external(ordinary), ordinary)

    def test_runtime_accepts_optional_journal_resolved_with_extended_dos_prefix(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            config, _ = initialize(Path(directory) / 'runtime', client_id='synthetic', entry='test')
            journal = config.parent / 'memory.sqlite3-shm'
            with resolved_as(journal, Path('\\\\?\\' + str(journal))):
                self.assertEqual(MemoryRuntime(config).root, config.parent)

    def test_extended_dos_alias_does_not_bypass_checkout_exclusion(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            alias = Path(directory) / 'alias'
            checkout = Path(__file__).resolve().parents[2] / 'synthetic-forbidden-runtime'
            with resolved_as(alias, Path('\\\\?\\' + str(checkout))):
                with self.assertRaisesRegex(MemoryFailure, 'runtime_inside_checkout'):
                    external(alias)

    def test_retargeted_credential_and_extended_unc_are_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            root = Path(directory)
            config, _ = initialize(root / 'runtime', client_id='synthetic', entry='test')
            credential = config.parent / 'client.json'
            with resolved_as(credential, Path('\\\\?\\' + str(root / 'outside.json'))):
                with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_path'):
                    MemoryRuntime(config)
        unc = Path('\\\\?\\UNC\\synthetic.invalid\\share\\runtime')
        with resolved_as(unc, unc), patch('evernook.paths.ctypes.windll.kernel32.GetDriveTypeW', return_value=4):
            with self.assertRaisesRegex(MemoryFailure, 'local_fixed_ntfs_required'):
                external(unc)

    def test_optional_journal_disappearing_between_exists_and_acl_is_rechecked(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            config, _ = initialize(Path(directory) / 'runtime', client_id='synthetic', entry='test')
            journal = config.parent / 'memory.sqlite3-shm'
            journal.write_bytes(b'synthetic optional journal fixture')
            roots_checked = []

            def during_acl(path: Path, sid: bytes, *, protected: bool) -> None:
                if protected:
                    roots_checked.append(path)
                if path == journal:
                    journal.unlink()
                verify_dacl(path, sid, protected=protected)

            with patch('evernook.paths.verify_dacl', side_effect=during_acl):
                verify_private_directory(config.parent)
            self.assertEqual(roots_checked, [config.parent, config.parent])
            self.assertFalse(journal.exists())

    def test_only_a_missing_journal_retries_and_the_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            root = Path(directory) / 'runtime'
            provision_private_directory(root)
            journal = root / 'memory.sqlite3-wal'
            credential = root / 'client.json'
            journal.touch()
            credential.touch()
            for target, status, attempts in ((journal, 5, 1), (credential, 2, 1), (journal, 2, 3)):
                roots_checked: list[Path] = []

                def during_acl(path: Path, sid: bytes, *, protected: bool) -> None:
                    if protected:
                        roots_checked.append(path)
                    if path == target:
                        raise ctypes.WinError(status)
                    verify_dacl(path, sid, protected=protected)

                with self.subTest(target=target.name, status=status), patch(
                        'evernook.paths.verify_dacl', side_effect=during_acl):
                    with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                        verify_private_directory(root)
                    self.assertEqual(len(roots_checked), attempts)

    def test_inherited_private_child_is_accepted_but_root_requires_protection(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            root = external(Path(directory) / '专用目录')
            provision_private_directory(root)
            child = root / '继承目录'
            child.mkdir()
            with patch('evernook.paths.subprocess.run', side_effect=AssertionError('unexpected process')):
                verify_private_directory(root, extra_files=(child,))
                with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                    verify_private_directory(child)
                with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                    verify_private_directory(child / 'missing')

    def test_only_plain_current_user_grants_with_effective_full_control_pass(self) -> None:
        sid = bytes.fromhex('010100000000000515000000')
        other = bytes.fromhex('010100000000000516000000')

        def ace(kind: int, flags: int, rights: int, identity: bytes = sid) -> bytes:
            return struct.pack('<BBHI', kind, flags, 8 + len(identity), rights) + identity

        full, partial = 0x1F01FF, 0x120089
        allow = ace(0, 0, full)
        for entries, accepted in (
            ([], False), ([allow], True), ([ace(0, 0x10, full)], True),
            ([ace(0, 0, partial), allow], True), ([ace(0, 0, partial)], False),
            ([ace(0, 8, full)], False), ([allow, ace(1, 0, partial)], False),
            ([allow, ace(0, 0, partial, other)], False), ([allow, ace(9, 0, full)], False),
        ):
            body = b''.join(entries)
            acl = struct.pack('<BBHHH', 2, 0, 8 + len(body), len(entries), 0) + body
            with self.subTest(entries=entries):
                self.assertEqual(_current_user_only(acl, sid), accepted)
