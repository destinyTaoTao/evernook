from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
import subprocess
import sys
import sqlite3
import struct
import math
from contextlib import closing
import tempfile
import threading
import unittest
from unittest.mock import patch

from evernook import Access, Change, EmbeddingSpace, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize


class SyntheticEmbedding:
    space = EmbeddingSpace("synthetic-only", "v1", 3, "synthetic-digest", 64, 16)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.error: str | None = None
        self.hook: Callable[[], object] | None = None
        self.invalid: tuple[float, ...] | None = None
        self.none_for: str | None = None

    def embed(self, texts: tuple[str, ...], *, timeout: float, cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        self.calls.append(texts)
        if self.hook:
            self.hook()
        if self.error:
            raise MemoryFailure(self.error)
        if self.invalid is not None:
            return tuple(self.invalid for _ in texts)
        return tuple(None if self.none_for is not None and self.none_for in text else
                     (1.0, 0.0, 0.0) if any(word in text for word in ("辛辣", "饮食偏好", "终点", "跨年承诺"))
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

    def test_current_receipt_is_excluded_before_fusion_in_built_and_unbuilt_channels(self) -> None:
        earlier = self.save("合成：喜欢辛辣的食物。")
        current = self.save("合成：饮食偏好是什么？")
        for indexed in (False, True):
            if indexed:
                self.workflow.refresh(self.access)
                self.runtime.refresh_lexical(self.access)
            result = self.workflow.recall(self.access, "饮食偏好", limit=1, current_input_id=current)
            self.assertNotIn(current, [hit.evidence.message_id for hit in result.hits])
            if indexed:
                self.assertEqual(result.hits[0].evidence.message_id, earlier)
                self.assertIn("semantic", result.hits[0].channels)
            else:
                self.assertEqual(result.hits, ())

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

    def test_status_reports_actual_vectors_before_a_multichunk_message_is_complete(self) -> None:
        self.save("甲" * 160)  # 64/16 configured space: [0,64), [48,112), [96,160).
        initial = self.workflow.status(self.access)
        self.assertEqual(initial.stored_vectors, 0)
        partial = self.workflow.refresh(self.access, limit=1)
        self.assertEqual(partial.indexed_messages, 0)
        self.assertEqual(partial.stored_vectors, 1)
        restarted = SemanticRecall(MemoryRuntime(self.config), self.backend)
        complete = restarted.refresh(self.access)
        self.assertEqual(complete.indexed_messages, 1)
        self.assertEqual(complete.stored_vectors, 3)

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

    def test_unembeddable_chunk_is_marked_explicitly_and_never_blocks_the_queue(self) -> None:
        blocked = self.save("合成：无法向量化🙂🙂🙂")
        follower = self.save("合成：账单37.50元。")
        self.backend.none_for = "🙂🙂🙂"
        status = self.workflow.refresh(self.access)
        self.assertEqual((status.state, status.failed_chunks), ("ready", 1))
        self.assertEqual(status.indexed_messages, 2)
        calls = len(self.backend.calls)
        again = self.workflow.refresh(self.access)
        self.assertEqual(len(self.backend.calls), calls)  # Failure marks are excluded from pending: no re-embed churn.
        self.assertEqual(again.failed_chunks, 1)
        page = self.workflow.recall(self.access, "账单")
        self.assertIn(follower, [hit.evidence.message_id for hit in page.hits])
        self.assertNotIn(blocked, [hit.evidence.message_id for hit in page.hits])
        # New content re-embeds the piece and clears the recorded failure.
        evidence = self.save("合成：更正上一条记录")
        self.runtime.change(self.access, Change("synthetic-correct", blocked, 1, "correct", evidence, "synthetic"))
        self.backend.none_for = None
        healed = self.workflow.refresh(self.access)
        self.assertEqual(healed.failed_chunks, 0)
        self.assertEqual(healed.state, "ready")

    def test_recent_window_returns_verified_rows_when_the_budget_fires_at_the_scan_limit(self) -> None:
        for index in range(257):
            self.save(f"合成：近期记录{index}", "2026-10-29T00:00:00Z")
        calls = {"count": 0}

        def check() -> float:
            calls["count"] += 1
            if calls["count"] == 257:
                raise MemoryFailure("recall_canceled")
            return 1.0

        since, expanded, chars, limited = self.workflow._recent(self.access, "2026-10-30T00:00:00Z", 8192, 7, check=check)
        self.assertTrue(limited)
        self.assertEqual(calls["count"], 256)  # The limit return precedes the 257th budget check.
        self.assertTrue(since.startswith("2026-10-23"))  # Dense recent content keeps the full 7-day floor.
        self.assertFalse(expanded)

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

    def test_full_history_candidate_beyond_ten_thousand_chunks_is_reachable(self) -> None:
        self.backend.space = replace(self.backend.space, chunk_size=32, overlap=31)
        self.workflow = SemanticRecall(self.runtime, self.backend)
        text = "x" * 10040 + "合成终点"
        target = self.save(text)
        self.workflow.refresh(self.access, limit=1)
        starts = list(range(0, len(text) - 31))
        positions = {0: (1., 0., 0.), 5002: (0., 1., 0.), starts[-1]: (0., 0., 1.)}
        with self.runtime._connection() as db:
            db.executemany("INSERT OR REPLACE INTO semantic_chunks_v1 VALUES (?,?,?,?,?,?,?)", [
                (self.backend.space.key, target, start, min(len(text), start + 32), 1, 1,
                 struct.pack("<3f", *positions.get(start, (-1 / math.sqrt(3),) * 3))) for start in starts])
            db.execute("UPDATE semantic_documents_v1 SET chunk_count=?", (len(starts),))
        result = self.workflow.recall(self.access, "合成第三类查询", as_of="2026-10-30T00:00:00Z", recent_target_chars=1)
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertGreater(result.hits[0].evidence.spans[0].start, 9999)
        self.assertFalse(result.semantic_scan_limited)
        for position, vector in positions.items():
            hits, scanned, limited, _ = self.workflow._semantic(self.access, vector, since=None, until=None, minimum=.4)
            self.assertEqual(hits[0][0].spans[0].start, position)
            self.assertEqual(scanned, len(starts))
            self.assertFalse(limited)

    def test_long_input_keeps_explicit_intent_and_checks_the_entire_original(self) -> None:
        target = self.save("合成：辛辣的食物")
        self.workflow.refresh(self.access)
        result = self.workflow.recall(self.access, "irrelevant " * 100 + "最终问题：饮食偏好？", query_intent="饮食偏好")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        calls = len(self.backend.calls)
        with self.assertRaisesRegex(MemoryFailure, "protected_query"):
            self.workflow.recall(self.access, "password=synthetic-secret " + "x" * 400, query_intent="饮食偏好")
        self.assertEqual(len(self.backend.calls), calls)

    def test_long_input_without_explicit_intent_discloses_bounded_tail_plan(self) -> None:
        target = self.save("合成：辛辣的食物")
        self.workflow.refresh(self.access)
        result = self.workflow.recall(self.access, "无关背景。" * 100 + "真正的问题：饮食偏好？")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertIn("query_coverage_limited", result.degraded)
        assert result.query_plan is not None
        self.assertEqual(result.query_plan.kind, "bounded_tail")

    def test_optional_native_and_python_search_agree_without_a_second_index(self) -> None:
        self.save("合成：辛辣")
        self.save("合成：账单")
        self.workflow.refresh(self.access)
        native = self.workflow.recall(self.access, "饮食偏好")
        with patch("evernook.vector_distance.importlib.import_module", side_effect=ImportError):
            fallback = self.workflow.recall(self.access, "饮食偏好")
        self.assertEqual(native.hits, fallback.hits)
        self.assertEqual(fallback.semantic_search_backend, "python-exact")
        self.assertEqual(native.semantic_scanned_chunks, fallback.semantic_scanned_chunks)
        self.assertFalse(fallback.semantic_scan_limited)

    def test_sql_search_deduplicates_source_chunks_before_candidate_limit(self) -> None:
        first = self.save("辛辣" * 3300)
        second = self.save("合成：辛辣")
        while self.workflow.status(self.access).pending_messages:
            self.workflow.refresh(self.access)
        result = self.workflow.recall(self.access, "饮食偏好")
        self.assertEqual({hit.evidence.message_id for hit in result.hits}, {first, second})

    def test_sql_scan_rechecks_cancellation_and_preserves_its_error_code(self) -> None:
        self.save("辛辣" * 3300)
        while self.workflow.status(self.access).pending_messages:
            self.workflow.refresh(self.access)
        calls = 0
        def check() -> float:
            nonlocal calls
            calls += 1
            if calls > 12:
                raise MemoryFailure("recall_canceled")
            return 10.
        for native in (True, False):
            calls = 0
            with patch("evernook.vector_distance.importlib.import_module", side_effect=ImportError) if not native else patch.dict("os.environ", {}):
                with self.assertRaisesRegex(MemoryFailure, "recall_canceled"):
                    self.workflow._semantic(self.access, (1., 0., 0.), since=None, until=None, minimum=.4, check=check)

    def test_corrupt_vector_does_not_count_as_complete_no_matching_evidence(self) -> None:
        self.save("合成：辛辣")
        self.workflow.refresh(self.access)
        with self.runtime._connection() as db:
            db.execute("UPDATE semantic_chunks_v1 SET vector=?", (struct.pack("<3f", 0., 0., 0.),))
        result = self.workflow.recall(self.access, "饮食偏好")
        self.assertTrue(result.degraded)
        self.assertEqual(result.evidence_state, "incomplete")

    def test_explicit_time_focus_filters_before_top_k_but_unknown_year_keeps_global_opportunity(self) -> None:
        old = self.save("合成：跨年承诺", "2019-01-01T00:00:00Z")
        current = self.save("合成：跨年承诺", "2025-01-01T00:00:00Z")
        unknown = self.save("合成：跨年承诺", None)
        self.workflow.refresh(self.access)
        focused = self.workflow.recall(self.access, "饮食偏好", since="2024-01-01T00:00:00Z", until="2026-01-01T00:00:00Z")
        self.assertEqual({hit.evidence.message_id for hit in focused.hits}, {current})
        unfocused = self.workflow.recall(self.access, "饮食偏好")
        self.assertEqual({hit.evidence.message_id for hit in unfocused.hits}, {old, current, unknown})

    def test_cancel_after_source_validation_prevents_small_batch_vector_or_failure_commit(self) -> None:
        self.save("合成辛辣")
        original_state = self.runtime._state
        for unembeddable in (False, True):
            canceled = threading.Event()
            def stop_after_validation(db: sqlite3.Connection, message_id: str) -> object:
                state = original_state(db, message_id)
                canceled.set()
                return state
            self.backend.none_for = "合成辛辣" if unembeddable else None
            with patch.object(self.runtime, "_state", side_effect=stop_after_validation):
                with self.assertRaisesRegex(MemoryFailure, "semantic_refresh_canceled"):
                    self.workflow.refresh(self.access, limit=1, cancel=canceled)
            status = self.workflow.status(self.access)
            self.assertEqual(status.stored_vectors, 0)
            self.assertEqual(status.failed_chunks, 0)

    def test_ready_keywords_retrieve_a_natural_question_below_the_semantic_threshold(self) -> None:
        target = self.save("合成：我在银叶桥学会了独木舟。")
        self.runtime.refresh_lexical(self.access)
        without_model = SemanticRecall(self.runtime)
        result = without_model.recall(self.access, "我在哪里学会水上划行？")
        self.assertEqual(result.hits[0].evidence.message_id, target)
        self.assertIn("keyword", result.hits[0].channels)
        confirmation = self.save("合成：确认不再召回")
        self.runtime.change(self.access, Change("keyword-hide", target, 1, "suppress", confirmation, "synthetic"))
        self.assertEqual(without_model.recall(self.access, "我在哪里学会水上划行？").hits, ())

    def test_keyword_channel_filters_time_before_top_k_and_ignores_question_stopwords(self) -> None:
        old = self.save("合成：在银叶桥学会了独木舟。", "2019-01-01T00:00:00Z")
        new = self.save("合成：在星湖学会了独木舟。", "2025-01-01T00:00:00Z")
        self.runtime.refresh_lexical(self.access)
        recall = SemanticRecall(self.runtime)
        page = recall.recall(self.access, "我在哪里学会水上划行？", since="2024-01-01T00:00:00Z")
        self.assertEqual({hit.evidence.message_id for hit in page.hits}, {new})
        self.assertNotIn(old, [hit.evidence.message_id for hit in page.hits])
        self.assertEqual(recall.recall(self.access, "我在哪里？").hits, ())

    def test_duplicate_fts_rows_do_not_take_candidate_seats_or_add_rrf_votes(self) -> None:
        first = self.save("合成：学会。")
        second = self.save("合成：我曾在银叶桥旁的夏季训练营学会了独木舟。")
        self.runtime.refresh_lexical(self.access)
        with self.runtime._connection() as db:
            db.executemany("INSERT INTO lexical(message_id,terms) SELECT message_id,terms FROM lexical WHERE message_id=? LIMIT 1", [(first,)] * 110)
        self.assertEqual(self.runtime.lexical_status(self.access).state, "ready")
        result = SemanticRecall(self.runtime).recall(self.access, "我在哪里学会水上划行？")
        self.assertEqual({hit.evidence.message_id for hit in result.hits}, {first, second})
        self.assertTrue(all(hit.channels.count("keyword") == 1 for hit in result.hits))
        self.assertTrue(all(hit.score < .02 for hit in result.hits))


if __name__ == "__main__":
    unittest.main()
