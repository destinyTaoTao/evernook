from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest

from evernook import Access, Change, EmbeddingSpace, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize


class SyntheticEmbedding:
    space = EmbeddingSpace("synthetic-only", "v1", 3, "synthetic-digest", 64, 16)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.error: str | None = None
        self.hook: Callable[[], object] | None = None
        self.invalid: tuple[float, ...] | None = None

    def embed(self, texts: tuple[str, ...], *, timeout: float, cancel: threading.Event | None = None) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        if self.hook:
            self.hook()
        if self.error:
            raise MemoryFailure(self.error)
        if self.invalid is not None:
            return tuple(self.invalid for _ in texts)
        return tuple((1.0, 0.0, 0.0) if any(word in text for word in ("辛辣", "饮食偏好", "终点", "跨年承诺"))
                     else (0.0, 1.0, 0.0) if "账单" in text else (0.0, 0.0, 1.0) for text in texts)


class SemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-semantic-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.backend = SyntheticEmbedding()
        self.workflow = SemanticRecall(self.runtime, self.backend)
        self.sequence = 0

    def save(self, text: str, when: str | None = "2021-01-01T00:00:00Z", *, sensitive: bool = False) -> str:
        self.sequence += 1
        return self.runtime.submit(self.access, Event(Source("test", "session", "main", str(self.sequence)),
            "user", self.sequence, when, "complete", text, sensitive)).message_id

    def test_semantic_paraphrase_fuses_with_lexical_and_references_exact_source(self) -> None:
        target = self.save("合成：小林喜欢辛辣的食物🙂。")
        self.save("合成：账单37.50元。")
        self.assertEqual(self.workflow.refresh(self.access).state, "ready")
        self.runtime.refresh_lexical(self.access)
        self.assertEqual(self.runtime.search(self.access, "饮食偏好").hits, ())
        result = self.workflow.recall(self.access, "饮食偏好", as_of="2026-10-30T00:00:00Z")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertIn("semantic", result.hits[0].channels)
        for span in result.hits[0].evidence.spans:
            saved = self.runtime.read(self.access, target)
            assert saved is not None
            self.assertEqual(saved.event.text[span.start:span.end], span.text)
        literal = self.workflow.recall(self.access, "辛辣")
        self.assertIn("exact", literal.hits[0].channels)

    def test_partial_chunks_resume_after_restart_and_preserve_unicode_coordinates(self) -> None:
        target = self.save("甲🙂e\u0301" * 70 + "合成终点")
        status = self.workflow.refresh(self.access, limit=1)
        self.assertEqual((status.state, status.pending_messages), ("behind", 1))
        restarted = SemanticRecall(MemoryRuntime(self.config), self.backend)
        self.assertEqual(restarted.refresh(self.access).state, "ready")
        hit = restarted.recall(self.access, "饮食偏好").hits[0].evidence
        self.assertEqual(hit.message_id, target)
        self.assertIn("终点", hit.spans[0].text)
        self.assertEqual(restarted.refresh(self.access).indexed_messages, 1)

    def test_sparse_recent_window_expands_by_content_dense_window_keeps_global_history(self) -> None:
        target = self.save("合成：跨年承诺，喜欢辛辣。")
        self.save("合成：近期短记录", "2026-10-29T00:00:00Z")
        self.workflow.refresh(self.access)
        sparse = self.workflow.recall(self.access, "饮食偏好", as_of="2026-10-30T00:00:00Z", recent_target_chars=100)
        self.assertTrue(sparse.recent_expanded)
        self.assertTrue(sparse.recent_since.startswith("2021-01-01"))
        self.assertEqual(sparse.hits[0].evidence.message_id, target)
        self.save("合成无关天气" * 30, "2026-10-29T12:00:00Z")
        self.workflow.refresh(self.access)
        dense = self.workflow.recall(self.access, "饮食偏好", as_of="2026-10-30T00:00:00Z", recent_target_chars=100)
        self.assertFalse(dense.recent_expanded)
        self.assertTrue(dense.recent_since.startswith("2026-10-23"))
        self.assertEqual([hit.evidence.message_id for hit in dense.hits], [target])
        self.assertNotIn("recent_semantic", dense.hits[0].channels)

    def test_unknown_and_future_times_remain_global_evidence_without_recent_labels(self) -> None:
        unknown = self.save("合成：跨年承诺未知时间", None)
        future = self.save("合成：跨年承诺计划", "2030-01-01T00:00:00Z")
        self.workflow.refresh(self.access)
        result = self.workflow.recall(self.access, "饮食偏好", as_of="2026-10-30T00:00:00Z")
        self.assertEqual({hit.evidence.message_id for hit in result.hits}, {unknown, future})
        self.assertTrue(all(not any(channel.startswith("recent_") for channel in hit.channels) for hit in result.hits))

    def test_model_failure_space_change_and_disabled_mode_return_literal_evidence_with_degradation(self) -> None:
        target = self.save("合成辛辣")
        self.workflow.refresh(self.access)
        self.backend.error = "embedding_timeout"
        result = self.workflow.recall(self.access, "辛辣")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertIn("embedding_timeout", result.degraded)
        other = SyntheticEmbedding()
        other.space = replace(other.space, revision="v2")
        changed = SemanticRecall(self.runtime, other)
        self.assertEqual(changed.status(self.access).indexed_messages, 0)
        self.assertEqual(changed.recall(self.access, "饮食偏好").hits, ())
        self.assertEqual(changed.refresh(self.access).state, "ready")
        disabled = SemanticRecall(self.runtime).recall(self.access, "辛辣")
        self.assertEqual(disabled.hits[0].evidence.message_id, target)
        self.assertIn("semantic_disabled", disabled.degraded)

    def test_suppression_during_embedding_rolls_back_and_old_vectors_never_reappear(self) -> None:
        target = self.save("合成辛辣")
        evidence = self.save("合成：不再召回上一条")
        self.backend.hook = lambda: self.runtime.change(self.access, Change("synthetic-change", target, 1, "suppress", evidence, "synthetic"))
        with self.assertRaisesRegex(MemoryFailure, "stale_source"):
            self.workflow.refresh(self.access)
        self.backend.hook = None
        self.workflow.refresh(self.access)
        self.assertEqual(self.workflow.recall(self.access, "饮食偏好").hits, ())
        self.assertEqual(self.workflow.status(self.access).source_messages, 1)

    def test_existing_vectors_filter_suppression_and_no_query_cache_revives_evidence(self) -> None:
        target = self.save("合成辛辣")
        evidence = self.save("合成：不再召回上一条")
        self.workflow.refresh(self.access)
        self.assertTrue(self.workflow.recall(self.access, "饮食偏好").hits)
        self.runtime.change(self.access, Change("synthetic-change", target, 1, "suppress", evidence, "synthetic"))
        self.assertEqual(self.workflow.recall(self.access, "饮食偏好").hits, ())

    def test_projections_protected_queries_and_invalid_vectors_never_reach_success(self) -> None:
        self.save("synthetic opaque protected", sensitive=True)
        self.assertEqual(self.workflow.refresh(self.access).source_messages, 0)
        self.assertEqual(self.backend.calls, [])
        with self.assertRaisesRegex(MemoryFailure, "protected_query"):
            self.workflow.recall(self.access, "password=synthetic-secret")
        self.assertEqual(self.backend.calls, [])
        self.save("合成辛辣")
        for invalid in [(0.0, 0.0, 0.0), (float("nan"), 0.0, 1.0), (1.0,)]:
            self.backend.invalid = invalid
            with self.assertRaisesRegex(MemoryFailure, "invalid_embedding"):
                self.workflow.refresh(self.access)
            self.assertEqual(self.workflow.status(self.access).indexed_messages, 0)

    def test_embedding_holds_no_write_transaction_and_every_operation_authenticates(self) -> None:
        self.save("合成辛辣")
        self.backend.hook = lambda: self.save("合成：并发新消息")
        self.workflow.refresh(self.access)
        self.backend.hook = None
        pending = self.workflow.status(self.access).pending_messages
        assert pending is not None
        self.assertGreater(pending, 0)
        wrong = Access(self.access.client_id, "synthetic-wrong-token")
        calls = len(self.backend.calls)
        for operation in (lambda: self.workflow.status(wrong), lambda: self.workflow.refresh(wrong),
                          lambda: self.workflow.recall(wrong, "饮食偏好")):
            with self.assertRaisesRegex(MemoryFailure, "unauthorized"):
                operation()
        self.assertEqual(len(self.backend.calls), calls)

    def test_process_exit_before_or_after_vector_commit_recovers_without_losing_original(self) -> None:
        worker = Path(__file__).with_name("crash_worker.py")
        interrupted = subprocess.run([sys.executable, str(worker), str(self.config), "semantic_before_commit"],
                                     capture_output=True, timeout=30)
        self.assertEqual(interrupted.returncode, 86, interrupted.stderr)
        self.assertEqual(self.workflow.status(self.access).indexed_messages, 0)
        self.assertEqual(self.runtime.status(self.access).messages, 1)
        self.assertEqual(self.workflow.refresh(self.access).state, "ready")
        committed = subprocess.run([sys.executable, str(worker), str(self.config), "semantic_after_commit"],
                                   capture_output=True, timeout=30)
        self.assertEqual(committed.returncode, 87, committed.stderr)
        self.assertEqual(self.workflow.status(self.access).indexed_messages, 1)
        self.assertEqual(len(self.workflow.recall(self.access, "合成中断片段").hits), 1)

    def test_unavailable_semantic_metadata_does_not_block_literal_evidence(self) -> None:
        target = self.save("合成辛辣")
        self.workflow.refresh(self.access)
        with closing(sqlite3.connect(self.config.parent / "memory.sqlite3")) as db:
            db.execute("ALTER TABLE semantic_documents_v1 RENAME COLUMN chunk_count TO synthetic_broken_column")
        result = self.workflow.recall(self.access, "辛辣")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertIn("semantic_unavailable", result.degraded)
        self.assertEqual(result.semantic.state, "unavailable")


if __name__ == "__main__":
    unittest.main()
