from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from evernook import MemoryFailure, MemoryRuntime, initialize
from evernook.paths import external, verify_private_directory


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
            actual = subprocess.run

            def during_acl(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                command = list(args)
                command[-1] = command[-1].replace('[IO.File]::GetAccessControl($path)',
                    "$(if($path -eq [IO.Path]::Combine($paths[0], 'memory.sqlite3-shm')) { [IO.File]::Delete($path) }; [IO.File]::GetAccessControl($path))")
                return actual(command, **kwargs)

            with patch('evernook.paths.subprocess.run', side_effect=during_acl) as run:
                verify_private_directory(config.parent)
                self.assertEqual(run.call_count, 2)
            self.assertFalse(journal.exists())

    def test_real_acl_failure_and_exhausted_journal_rechecks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-path-') as directory:
            config, _ = initialize(Path(directory) / 'runtime', client_id='synthetic', entry='test')
            for status, calls in ((1, 1), (2, 3)):
                with self.subTest(status=status), patch('evernook.paths.subprocess.run',
                        side_effect=subprocess.CalledProcessError(status, ['synthetic-acl-check'])) as run:
                    with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                        verify_private_directory(config.parent)
                    self.assertEqual(run.call_count, calls)
