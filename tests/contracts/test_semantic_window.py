from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
import tempfile
import threading
import unittest

from evernook import Change, EmbeddingSpace, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize


class WindowEmbedding:
    space = EmbeddingSpace('synthetic-window', 'v1', 3, 'synthetic-digest', 64, 16)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.hook: Callable[[], None] | None = None

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        if self.hook:
            self.hook()
        return tuple((1.0, 0.0, 0.0) for _ in texts)


class SemanticWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-semantic-window-')
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / 'runtime', client_id='synthetic', entry='test')
        self.runtime = MemoryRuntime(self.config)
        self.ids = tuple(receipt.message_id for receipt in self.runtime.submit_many(self.access, tuple(
            Event(Source('test', 'window', 'main', str(index)), 'user', index, '2021-01-01T00:00:00Z',
                  'complete', f'合成窗口记录{index}') for index in range(37))))
        self.backend = WindowEmbedding()
        self.recall = SemanticRecall(self.runtime, self.backend)

    def test_bounded_window_batches_inference_and_resumes_exact_coverage(self) -> None:
        first = self.recall.refresh_window(self.access, limit=33)
        self.assertEqual((first.indexed_messages, first.pending_messages), (33, 4))
        self.assertEqual([len(call) for call in self.backend.calls], [16, 16, 1])
        restarted = SemanticRecall(MemoryRuntime(self.config), self.backend)
        self.assertEqual(restarted.refresh_window(self.access).state, 'ready')
        before = len(self.backend.calls)
        self.assertEqual(restarted.refresh_window(self.access).stored_vectors, 37)
        self.assertEqual(len(self.backend.calls), before)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_page'):
            self.recall.refresh(self.access, limit=17)
        with self.assertRaisesRegex(MemoryFailure, 'invalid_page'):
            self.recall.refresh_window(self.access, limit=1001)

    def test_cancellation_keeps_committed_batches_and_retry_fills_remaining(self) -> None:
        cancel = threading.Event()
        def interrupt() -> None:
            if len(self.backend.calls) == 2:
                cancel.set()
        self.backend.hook = interrupt
        with self.assertRaisesRegex(MemoryFailure, 'semantic_refresh_canceled'):
            self.recall.refresh_window(self.access, cancel=cancel)
        self.assertEqual(self.recall.status(self.access).stored_vectors, 16)
        self.backend.hook = None
        self.assertEqual(self.recall.refresh_window(self.access).stored_vectors, 37)

    def test_source_hidden_during_inference_cannot_publish_stale_vectors(self) -> None:
        def hide() -> None:
            target = self.ids[0]
            self.runtime.change(self.access, Change('hide-window', target, 1, 'suppress', self.ids[1], 'synthetic control'))
        self.backend.hook = hide
        with self.assertRaisesRegex(MemoryFailure, 'stale_source'):
            self.recall.refresh_window(self.access)
        self.assertEqual(self.recall.status(self.access).stored_vectors, 0)
        self.backend.hook = None
        self.assertEqual(self.recall.refresh_window(self.access).indexed_messages, 36)
