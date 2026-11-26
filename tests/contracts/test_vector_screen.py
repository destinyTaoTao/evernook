from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import importlib.util
import math
from pathlib import Path
import random
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize
from evernook import vector_screen as screen
from evernook.purge import PurgeDecision, PurgeWorkflow
from evernook.vector_distance import distance_function
from tests.contracts.test_semantic import SyntheticEmbedding


class VectorScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="evernook-int8-screen-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config, self.access = initialize(self.root / "data", client_id="synthetic", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(config)
        self.workflow = SemanticRecall(self.runtime, SyntheticEmbedding())
        self.next_id = 0

    def save(self, text: str, when: str = "2022-01-01T00:00:00Z") -> str:
        self.next_id += 1
        return self.runtime.submit(self.access, Event(Source("test", "screen", "main", str(self.next_id)), "user",
            self.next_id, when, "complete", text)).message_id

    def build(self) -> None:
        while self.workflow.status(self.access).pending_messages:
            self.workflow.refresh(self.access)
        self.workflow.configure_vector_screen(self.access, enabled=True)

    def compare(self, *, screened: bool, threshold: float = .4, current: str | None = None) -> tuple[int, int]:
        def run() -> tuple[object, object, int, str]:
            result = self.workflow._semantic(self.access, (1., 0., 0.), since=None, until=None, minimum=threshold,
                recent=("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"), current_input_id=current)
            return result.global_hits, result.recent_hits, result.scanned, result.engine
        with patch.object(screen, "candidates", return_value=None):
            legacy = run()
        candidate = run()
        self.assertEqual(legacy[:2], candidate[:2])
        self.assertEqual(candidate[3].endswith("-int8-bound-exact"), screened)
        return candidate[2], legacy[2]

    @unittest.skipUnless(importlib.util.find_spec("sqlite_vec"), "optional C cosine unavailable")
    def test_ties_multichunk_recent_outside_global_eligibility_and_changes(self) -> None:
        for i in range(103):
            self.save("辛辣" * 50 if i == 0 else "合成辛辣" + str(i))
        recent = self.save("合成辛辣近期", "2025-02-01T00:00:00Z")
        unrelated = self.save("普通账单")
        self.build()
        count, legacy = self.compare(screened=True)
        self.assertLess(count, legacy)
        self.compare(screened=True, threshold=1.0, current=recent)
        self.runtime.change(self.access, Change("hide", recent, 1, "suppress", unrelated, "synthetic"))
        self.compare(screened=True)
        with self.runtime._connection() as db:
            db.execute("UPDATE semantic_chunks_v1 SET vector=? WHERE message_id=?",
                       (struct.pack("<3f", 1, 0, 0), unrelated))
        self.compare(screened=True)
        self.assertGreaterEqual(self.workflow.refresh_vector_screen(self.access), 1)
        self.compare(screened=True)
        self.compare(screened=False, threshold=0.0)
        with patch.object(screen, "_MAX_CANDIDATES", 2):
            self.compare(screened=False)

    @unittest.skipUnless(importlib.util.find_spec("sqlite_vec"), "optional C cosine unavailable")
    def test_missing_tracking_tampering_queue_limit_rebuild_and_cancellation(self) -> None:
        message = self.save("普通账单")
        self.build()
        self.compare(screened=True)
        space = SyntheticEmbedding.space.key
        with self.runtime._connection() as snapshot:
            snapshot.execute("BEGIN")
            _, query, _ = distance_function(snapshot, (1., 0., 0.))
            self.assertEqual(screen.candidates(snapshot, space, query, .4), [])
            with self.runtime._connection() as writer:
                writer.execute("UPDATE semantic_chunks_v1 SET vector=? WHERE message_id=?", (query, message))
            self.assertEqual(screen.candidates(snapshot, space, query, .4), [])
        with self.runtime._connection() as fresh:
            fresh.execute("BEGIN")
            distance_function(fresh, (1., 0., 0.))
            self.assertEqual(screen.candidates(fresh, space, query, .4), [message])
        with patch.object(screen, "_MAX_DIRTY", 0):
            self.compare(screened=False)
        with self.runtime._connection() as db:
            db.execute(f"DELETE FROM {screen._DIRTY}")
        self.compare(screened=False)
        self.workflow.configure_vector_screen(self.access, enabled=True, rebuild=True)
        self.compare(screened=True)
        with self.runtime._connection() as db:
            db.execute(f"DROP TRIGGER {screen._PREFIX}update")
        self.compare(screened=False)
        self.workflow.configure_vector_screen(self.access, enabled=True, rebuild=True)
        def cancel() -> float:
            raise MemoryFailure("service_canceled")
        with patch.object(screen, "_encoded_rows", side_effect=MemoryFailure("service_canceled")):
            with self.assertRaisesRegex(MemoryFailure, "service_canceled"):
                self.workflow.configure_vector_screen(self.access, enabled=True, rebuild=True)
        self.compare(screened=True)
        with self.assertRaisesRegex(MemoryFailure, "service_canceled"):
            self.workflow.configure_vector_screen(self.access, enabled=False, check=cancel)
        self.compare(screened=True)
        different = SyntheticEmbedding()
        different.space = replace(different.space, revision="other")
        with self.assertRaisesRegex(MemoryFailure, "vector_screen_space_mismatch"):
            SemanticRecall(self.runtime, different).refresh_vector_screen(self.access)

    def test_export_and_purge_remove_compact_copies(self) -> None:
        target = self.save("辛辣")
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.access, "screen-purge", (target,))
        decision = PurgeDecision(preview.request_id, self.save("普通确认"))
        self.build()
        archive = self.root / "archive"
        marker = b"synthetic-compact-encoding-for-archive-removal" * 3
        with self.runtime._connection() as db:
            db.execute(f"UPDATE {screen._ROWS} SET code=? WHERE message_id=?", (marker, target))
        self.runtime.export_archive(self.access, archive)
        with closing(sqlite3.connect(archive / "evidence.sqlite3")) as db:
            self.assertFalse(screen._owned(db))
        self.assertNotIn(marker, (archive / "evidence.sqlite3").read_bytes())
        self.assertEqual(purge.purge(self.access, decision).state, "completed")
        with self.runtime._connection() as db:
            for table in (screen._ROWS, screen._DIRTY):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE message_id=?", (target,)).fetchone())
        self.workflow.configure_vector_screen(self.access, enabled=False)
        with self.runtime._connection() as db:
            self.assertFalse(screen._owned(db))

    @unittest.skipUnless(importlib.util.find_spec("sqlite_vec"), "optional C cosine unavailable")
    def test_new_source_threshold_equality_dirty_malformed_and_time_window(self) -> None:
        self.save("普通账单")
        self.build()
        message = self.save("合成辛辣", "2025-04-07T00:00:00Z")
        self.workflow.refresh(self.access)
        self.compare(screened=True, threshold=1.0)
        self.workflow.refresh_vector_screen(self.access)
        with self.runtime._connection() as db:
            db.execute("UPDATE semantic_chunks_v1 SET vector=? WHERE message_id=?", (struct.pack("<3f", 1, 1, 0), message))
        # Finite nonunit legacy data uses its authoritative native score.
        self.compare(screened=False)
        self.workflow.refresh_vector_screen(self.access)
        self.compare(screened=False)
        with self.runtime._connection() as db:
            db.execute("UPDATE semantic_chunks_v1 SET vector=? WHERE message_id=?", (struct.pack("<3f", 1, 0, 0), message))
        self.workflow.refresh_vector_screen(self.access)
        result = self.workflow._semantic(self.access, (1., 0., 0.), since="2025-04-07T00:00:00Z",
            until="2025-04-08T00:00:00Z", minimum=1.0, recent=None)
        self.assertEqual([hit.message_id for hit, _ in result.global_hits], [message])
        self.assertEqual(result.engine, "sqlite-vec-0.1.9-exact")

    @unittest.skipUnless(importlib.util.find_spec("sqlite_vec"), "optional C cosine unavailable")
    def test_native_distance_stays_within_bound_and_raw_int8_ties_can_mask_true_order(self) -> None:
        rng = random.Random(842730)
        with closing(sqlite3.connect(":memory:")) as db:
            for dim in (2, 3, 32, 384, 4096):
                for _ in range(5):
                    vectors = []
                    for _side in range(2):
                        values = tuple(rng.uniform(-1, 1) for _ in range(dim))
                        mag = math.hypot(*values)
                        normalized = tuple(x / mag for x in values)
                        _, raw, engine = distance_function(db, normalized)
                        if engine == "python-exact":
                            self.skipTest("pinned sqlite-vec unavailable")
                        code, error = screen.encode(raw, dim)
                        assert code is not None
                        vectors.append((raw, code, error))
                    (a, x, ra), (b, y, rb) = vectors
                    native, fast = db.execute("SELECT vec_distance_cosine(?,?),vec_distance_cosine(vec_int8(?),vec_int8(?))",
                                              (a, b, x, y)).fetchone()
                    self.assertLessEqual(abs(native - fast), ra + rb + 64 * dim * 2 ** -24)
            best = struct.pack("<2f", 1.0, 0.0)
            close = struct.pack("<2f", 1.0, .003)
            self.assertEqual(screen.encode(best, 2)[0], screen.encode(close, 2)[0])
            better, worse = db.execute("SELECT vec_distance_cosine(?,?),vec_distance_cosine(?,?)",
                                       (best, best, best, close)).fetchone()
            self.assertLess(better, worse)
