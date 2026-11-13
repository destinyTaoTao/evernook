from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Timer
import time
import unittest

from evernook import MemoryFailure, MemoryRuntime, initialize


class ConnectionSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-connection-')
        self.addCleanup(temporary.cleanup)
        config, _ = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='test')
        self.runtime = MemoryRuntime(config)

    @contextmanager
    def exclusive_lock(self) -> Iterator[Event]:
        ready, release = Event(), Event()

        def hold() -> None:
            with closing(sqlite3.connect(self.runtime.root / 'memory.sqlite3', isolation_level=None)) as db:
                db.execute('PRAGMA locking_mode=EXCLUSIVE')
                db.execute('BEGIN EXCLUSIVE')
                ready.set()
                try:
                    if not release.wait(5):
                        raise TimeoutError('synthetic lock holder deadline')
                finally:
                    db.execute('ROLLBACK')

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(hold)
            try:
                self.assertTrue(ready.wait(2), 'real SQLite lock was not acquired')
                yield release
            finally:
                release.set()
                pending.result(timeout=5)

    def test_short_real_lock_during_cancellable_connection_setup_is_waited_out(self) -> None:
        with self.exclusive_lock() as release:
            timer = Timer(.15, release.set)
            timer.start()
            try:
                deadline = time.monotonic() + 1
                with self.runtime._connection(check=lambda: deadline - time.monotonic()) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            finally:
                timer.cancel()
                timer.join()

    def test_setup_wait_observes_cancellation_and_search_deadline(self) -> None:
        for reason in ('search_canceled', 'search_timeout'):
            with self.subTest(reason=reason), self.exclusive_lock():
                stop = Event()
                timer = Timer(.03, stop.set)
                timer.start()
                checks = 0

                def budget() -> float:
                    nonlocal checks
                    checks += 1
                    if stop.is_set():
                        raise MemoryFailure(reason)
                    return 1

                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(MemoryFailure, reason):
                        with self.runtime._connection(check=budget):
                            self.fail('business query must not start before configuration succeeds')
                    self.assertGreater(checks, 1)
                    self.assertLess(time.monotonic() - started, .5)
                finally:
                    timer.cancel()
                    timer.join()

    def test_setup_wait_keeps_configured_busy_limit(self) -> None:
        self.runtime._timeout = .03
        with self.exclusive_lock():
            started = time.monotonic()
            with self.assertRaisesRegex(MemoryFailure, 'storage_busy'):
                with self.runtime._connection(check=lambda: 1):
                    self.fail('persistent lock must still fail')
            self.assertLess(time.monotonic() - started, .5)

    def test_business_statement_failure_is_not_retried(self) -> None:
        attempts = 0
        with self.assertRaisesRegex(MemoryFailure, 'storage_unavailable'):
            with self.runtime._connection(check=lambda: 1) as db:
                attempts += 1
                db.execute('SELECT synthetic_missing_column FROM messages')
        self.assertEqual(attempts, 1)

    def test_busy_business_transaction_is_not_retried(self) -> None:
        with closing(sqlite3.connect(self.runtime.root / 'memory.sqlite3', isolation_level=None)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            attempts = 0
            try:
                with self.assertRaisesRegex(MemoryFailure, 'storage_busy'):
                    with self.runtime._connection(check=lambda: 1) as db:
                        attempts += 1
                        db.execute('BEGIN IMMEDIATE')
                self.assertEqual(attempts, 1)
            finally:
                writer.execute('ROLLBACK')
