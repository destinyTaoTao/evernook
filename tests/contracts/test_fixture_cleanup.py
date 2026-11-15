from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests.contracts.fixture_cleanup import cleanup_fixture


class FixtureCleanupTests(unittest.TestCase):
    def test_shared_handle_release_retries_only_the_owned_temporary(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-cleanup-release-')
        target = Path(temporary.name) / 'lock'
        target.touch()
        reader = target.open('rb')
        refused = threading.Event()
        cleanup = temporary.cleanup

        def observed() -> None:
            try:
                cleanup()
            except PermissionError:
                refused.set()
                raise

        def release() -> None:
            if refused.wait(2):
                reader.close()

        releaser = threading.Thread(target=release)
        releaser.start()
        try:
            with patch.object(temporary, 'cleanup', side_effect=observed):
                cleanup_fixture(temporary)
            self.assertTrue(refused.is_set())
            self.assertFalse(target.parent.exists())
        finally:
            reader.close()
            releaser.join(timeout=3)
            cleanup()

    def test_persistent_shared_handle_remains_a_failure(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-cleanup-permanent-')
        target = Path(temporary.name) / 'lock'
        target.touch()
        try:
            with target.open('rb'), self.assertRaises(PermissionError):
                cleanup_fixture(temporary)
            self.assertTrue(target.exists())
        finally:
            temporary.cleanup()

    def test_permission_denial_is_not_ignored_or_retried(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-cleanup-denied-target-')
        error = PermissionError('synthetic access denied')
        error.winerror = 5
        try:
            with patch.object(temporary, 'cleanup', side_effect=error) as cleanup:
                with self.assertRaises(PermissionError):
                    cleanup_fixture(temporary)
            self.assertEqual(cleanup.call_count, 1)
        finally:
            temporary.cleanup()

    def test_workspace_path_is_rejected_before_any_cleanup(self) -> None:
        temporary = Mock(spec=tempfile.TemporaryDirectory)
        temporary.name = str(Path.cwd())
        with self.assertRaisesRegex(ValueError, 'not_an_owned_test_fixture'):
            cleanup_fixture(temporary)
        temporary.cleanup.assert_not_called()
