from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evernook import MemoryFailure
from evernook.runtime import _atomic_json


class EndpointPublicationTests(unittest.TestCase):
    def test_retry_publishes_only_after_actual_windows_reader_releases_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-endpoint-reader-') as temporary:
            target = Path(temporary) / 'memory-service.json'
            _atomic_json(target, {'generation': 'old'})
            refused = threading.Event()
            errors: list[Exception] = []
            replace = os.replace

            def observed(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                try:
                    replace(source, destination)
                except OSError:
                    refused.set()
                    raise

            def publish() -> None:
                try:
                    _atomic_json(target, {'generation': 'new'}, retry_replace=True)
                except Exception as error:
                    errors.append(error)

            writer = threading.Thread(target=publish)
            try:
                with target.open('rb'), patch('evernook.runtime.os.replace', side_effect=observed):
                    writer.start()
                    self.assertTrue(refused.wait(1), repr(errors))
                    self.assertEqual(json.loads(target.read_bytes()), {'generation': 'old'})
            finally:
                # Release the reader before joining, also on a failed assertion.
                if writer.ident is not None:
                    writer.join(timeout=2)
            self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(target.read_bytes()), {'generation': 'new'})
            self.assertEqual(list(target.parent.glob('.pair-*')), [])

    def test_default_and_expired_retry_keep_old_document_and_remove_staging_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-endpoint-deadline-') as temporary:
            target = Path(temporary) / 'memory-service.json'
            _atomic_json(target, {'generation': 'old'})
            with target.open('rb'):
                for retry in (False, True):
                    started = time.monotonic()
                    with self.assertRaisesRegex(MemoryFailure, 'client_provision_failed'):
                        _atomic_json(target, {'generation': 'new'}, retry_replace=retry)
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(json.loads(target.read_bytes()), {'generation': 'old'})
                    self.assertEqual(list(target.parent.glob('.pair-*')), [])

    def test_other_windows_replace_errors_are_not_retried(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-endpoint-error-') as temporary:
            target = Path(temporary) / 'memory-service.json'
            _atomic_json(target, {'generation': 'old'})
            error = OSError('synthetic invalid parameter')
            error.winerror = 87
            with patch('evernook.runtime.os.replace', side_effect=error) as replace, patch('evernook.runtime.time.sleep') as sleep:
                with self.assertRaisesRegex(MemoryFailure, 'client_provision_failed'):
                    _atomic_json(target, {'generation': 'new'}, retry_replace=True)
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(json.loads(target.read_bytes()), {'generation': 'old'})
            self.assertEqual(list(target.parent.glob('.pair-*')), [])
