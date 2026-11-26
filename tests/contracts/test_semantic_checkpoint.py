from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

from evernook import Capture, Change, Event, MemoryFailure, MemoryRuntime, SemanticRecall, Source, initialize
from evernook import semantic_checkpoint as cp
from evernook.purge import PurgeDecision, PurgeWorkflow
from tests.contracts.test_semantic import SyntheticEmbedding


class SemanticCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="evernook-semantic-checkpoint-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config, self.actor = initialize(self.root / "data", client_id="synthetic", entry="test", allow_archive_review=True)
        self.runtime = MemoryRuntime(config)
        self.backend = SyntheticEmbedding()
        self.workflow = SemanticRecall(self.runtime, self.backend)
        self.seq = 0

    def event(self, text: str) -> Event:
        self.seq += 1
        return Event(Source("test", "synthetic", "main", str(self.seq)), "user", self.seq, None, "complete", text)

    def save(self, text: str) -> str:
        return self.runtime.submit(self.actor, self.event(text)).message_id

    def parity(self, *, active: bool) -> None:
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            self.assertEqual(cp.status(db, self.backend.space.key) is not None, active)
            result = self.workflow._status(db)
            with patch.object(cp, "status", return_value=None):
                self.assertEqual(self.workflow._status(db), result)

    def test_clean_overlay_and_refresh_match_original_for_mixed_sources_and_spaces(self) -> None:
        target = self.save("合成辛辣" * 140)
        self.save("合成失败")
        self.save("")
        self.runtime.submit(self.actor, replace(self.event("合成敏感"), sensitive=True))
        self.backend.none_for = "失败"
        self.workflow.refresh(self.actor, limit=1)
        self.runtime.refresh_lexical(self.actor)
        self.runtime.configure_lexical_id_acceleration(self.actor, enabled=True)
        self.runtime.configure_lexical_checkpoint(self.actor, enabled=True)
        before = self.workflow.recall(self.actor, "合成辛辣", as_of="2026-11-25T00:00:00Z")
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        self.assertTrue(self.runtime.lexical_checkpoint_enabled(self.actor))
        self.assertEqual(self.workflow.recall(self.actor, "合成辛辣", as_of="2026-11-25T00:00:00Z"), before)
        self.parity(active=True)
        self.workflow.refresh_window(self.actor)
        self.assertEqual(self.workflow.status(self.actor).failed_chunks, 1)
        self.parity(active=True)
        generated = self.runtime.submit_capture(self.actor, Capture(replace(self.event("合成工具"), role="tool"),
            "tool_result", input_id=target, tool_name="evernook_memory_reflect")).message.message_id
        self.runtime.change(self.actor, Change("synthetic-hide", target, 1, "suppress", self.save("合成抑制"), "合成"))
        with self.runtime._connection() as db:
            db.execute("UPDATE captures SET manifest=json_set(manifest,'$.tool_name','other') WHERE message_id=?", (generated,))
        self.parity(active=True)
        self.assertGreater(self.workflow.refresh_coverage_checkpoint(self.actor, limit=2), 0)
        self.parity(active=True)
        self.workflow.refresh_coverage_checkpoint(self.actor)
        self.assertEqual(self.workflow.refresh_coverage_checkpoint(self.actor), 0)
        self.parity(active=True)
        other = SyntheticEmbedding()
        other.space = replace(other.space, revision="other-space")
        other_workflow = SemanticRecall(self.runtime, other)
        with self.runtime._connection() as db:
            self.assertIsNone(cp.status(db, other.space.key))
        self.assertEqual(other_workflow.status(self.actor).indexed_messages, 0)
        with self.assertRaisesRegex(MemoryFailure, "semantic_checkpoint_space_mismatch"):
            other_workflow.refresh_coverage_checkpoint(self.actor)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=False)
        self.assertTrue(self.runtime.lexical_checkpoint_enabled(self.actor))
        self.parity(active=False)

    def test_queue_bound_readonly_snapshot_and_missing_or_damaged_tracking(self) -> None:
        self.save("合成初始")
        self.workflow.refresh(self.actor)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        with self.runtime._connection() as reader:
            reader.execute("BEGIN")
            pinned = self.workflow._status(reader)
            self.runtime.submit_many(self.actor, tuple(self.event("合成新来源") for _ in range(100)))
            self.parity(active=True)
            self.save("合成第 101 条")
            self.parity(active=False)
            self.assertEqual(self.workflow._status(reader), pinned)
        self.assertEqual(self.workflow.refresh_coverage_checkpoint(self.actor, limit=1), 1)
        self.parity(active=True)
        for sql in (f"UPDATE {cp._HEAD} SET sources=sources+1",
                    f"DELETE FROM {cp._DIRTY}",
                    f"UPDATE {cp._ROWS} SET received='invalid'",
                    f"DROP TRIGGER {cp._PREFIX}captures_update"):
            with self.subTest(sql=sql):
                self.save("合成脏来源")
                with self.runtime._connection() as db:
                    db.execute(sql)
                self.parity(active=False)
                self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
                self.parity(active=True)
        with self.runtime._connection() as db:
            db.execute(f"CREATE TRIGGER {cp._HEAD} AFTER INSERT ON captures BEGIN SELECT 1; END")
        self.parity(active=False)
        with self.assertRaisesRegex(MemoryFailure, "semantic_checkpoint_conflict"):
            self.workflow.configure_coverage_checkpoint(self.actor, enabled=False)

    def test_legacy_missing_failures_remains_readable_and_build_requires_explicit_upgrade(self) -> None:
        self.save("合成旧库")
        with self.assertRaisesRegex(MemoryFailure, "semantic_checkpoint_requires_index"):
            self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        self.assertEqual(self.workflow.status(self.actor).state, "not_built")
        baseline = self.workflow.refresh(self.actor)
        with self.runtime._connection() as db:
            db.execute("DROP TABLE semantic_failures_v1")
        self.assertEqual(self.workflow.status(self.actor), baseline)
        with self.assertRaisesRegex(MemoryFailure, "semantic_checkpoint_requires_index"):
            self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        self.assertEqual(self.workflow.refresh(self.actor), baseline)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        self.parity(active=True)

    def test_failed_rebuild_rolls_back_and_direct_version_edits_use_the_overlay(self) -> None:
        target = self.save("合成独立版本")
        self.workflow.refresh(self.actor)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        with self.runtime._connection() as db:
            db.execute("UPDATE semantic_chunks_v1 SET content_version=content_version+1 WHERE message_id=?", (target,))
        self.parity(active=True)
        self.assertEqual(self.workflow.status(self.actor).pending_messages, 1)
        connect = sqlite3.connect
        def fail_build(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = connect(*args, **kwargs)
            db.set_authorizer(lambda action, name, _column, _schema, _trigger:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == cp._ROWS else sqlite3.SQLITE_OK)
            return db
        with patch("sqlite3.connect", side_effect=fail_build), self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.workflow.configure_coverage_checkpoint(self.actor, enabled=True, rebuild=True)
        self.parity(active=True)
        self.assertEqual(self.workflow.refresh_coverage_checkpoint(self.actor), 1)
        with self.runtime._connection() as db:
            db.execute("UPDATE messages SET receipt=json_set(receipt,'$.content_version',2) WHERE id=?", (target,))
            db.execute("UPDATE semantic_documents_v1 SET content_version=2 WHERE message_id=?", (target,))
        self.parity(active=True)
        self.assertEqual(self.workflow.status(self.actor).indexed_messages, 1)

    def test_stored_vectors_include_orphans_and_clean_check_does_not_read_source_or_vector_tables(self) -> None:
        self.save("合成孤立向量对照")
        self.workflow.refresh(self.actor)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        with self.runtime._connection() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("""INSERT INTO semantic_chunks_v1 SELECT space_id,'synthetic-orphan',start,end,
                state_version,content_version,vector FROM semantic_chunks_v1 LIMIT 1""")
        self.parity(active=True)
        self.assertEqual(self.workflow.refresh_coverage_checkpoint(self.actor), 1)
        self.parity(active=True)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True, rebuild=True)
        self.parity(active=True)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            db.set_authorizer(lambda action, table, _column, _schema, _trigger: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table in cp._SOURCES else sqlite3.SQLITE_OK)
            self.assertEqual(self.workflow._status(db).stored_vectors, 2)

    def test_export_omits_tracking_and_purge_removes_all_checkpoint_ids(self) -> None:
        target = self.save("合成待删")
        purge = PurgeWorkflow(self.runtime)
        preview = purge.preview(self.actor, "synthetic-purge", (target,))
        decision = PurgeDecision(preview.request_id, self.save("合成确认"))
        self.workflow.refresh(self.actor)
        self.workflow.configure_coverage_checkpoint(self.actor, enabled=True)
        self.assertEqual(self.runtime.export_archive(self.actor, self.root / "export").messages, 2)
        with closing(sqlite3.connect(self.root / "export" / "evidence.sqlite3")) as db:
            self.assertFalse(cp._owned(db))
        self.parity(active=True)
        self.assertEqual(purge.purge(self.actor, decision).state, "completed")
        with self.runtime._connection() as db:
            for table in (cp._ROWS, cp._DIRTY):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE message_id=?", (target,)).fetchone())
        self.parity(active=True)
