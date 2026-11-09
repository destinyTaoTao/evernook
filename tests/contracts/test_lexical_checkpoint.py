from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from evernook import Change, Event, MemoryFailure, MemoryRuntime, Receipt, Source, initialize, pair_client
from evernook import lexical_checkpoint as checkpoint
from evernook.purge import PurgeDecision, PurgeWorkflow
import evernook.runtime as implementation
import test_search


class CheckpointSearchContractTests(test_search.SearchTests):
    initial_fts_present = True

    def setUp(self) -> None:
        super().setUp()
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        self.runtime.configure_lexical_checkpoint(self.access, enabled=True)

    def test_unavailable_fts_dependency_does_not_block_exact_reads_or_new_originals(self) -> None:
        # A raw INSERT trigger makes SQLite check the lexical_docs foreign-key
        # child. Keep virtual-index failure distinct from that new write dependency;
        # the original no-checkpoint test remains unchanged in test_search.
        self.runtime.refresh_lexical(self.access)
        expected = self.runtime.search(self.access, "乌龙").hits
        real_connect = sqlite3.connect
        def deny_virtual(action: int, table: str | None, column: str | None, database: str | None, trigger: str | None) -> int:
            blocked = table is not None and table.startswith("lexical") and table != "lexical_docs"
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and blocked else sqlite3.SQLITE_OK
        def unavailable_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.set_authorizer(deny_virtual)
            return db
        with patch("sqlite3.connect", unavailable_connect):
            page = self.runtime.search(self.access, "乌龙")
            self.assertEqual(page.hits, expected)
            self.assertFalse(page.used_index)
            self.assertEqual(page.index.state, "unavailable")
            second = self.runtime.submit(self.access, replace(self.event,
                source=replace(self.event.source, event="during-index-failure"), text="合成：绿茶。"))
            self.assertEqual([hit.message_id for hit in self.runtime.search(self.access, "绿茶").hits], [second.message_id])
        self.assertEqual(self.runtime.rebuild_lexical(self.access).state, "ready")
        self.assertEqual(self.runtime.search(self.access, "乌龙").hits, expected)


class LexicalCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-checkpoint-")
        self.addCleanup(temporary.cleanup)
        self.config, self.actor = initialize(Path(temporary.name) / "data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "first"), "user", 1,
            "2021-01-01T00:00:00Z", "complete", "合成：紫罗兰与乌龙茶37.50。")
        self.receipt = self.runtime.submit(self.actor, self.event)
        self.runtime.refresh_lexical(self.actor)
        self.database = self.config.parent / "memory.sqlite3"

    def prepare(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.actor, enabled=True)
        self.assertTrue(self.runtime.configure_lexical_checkpoint(self.actor, enabled=True))

    def objects(self) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT name,type,sql FROM sqlite_master WHERE lower(name) GLOB ? ORDER BY name", (checkpoint._PREFIX + "*",)).fetchall()

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        with closing(sqlite3.connect(self.database)) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("messages", "lexical", "lexical_docs", checkpoint._ROWS, checkpoint._DIRTY, checkpoint._HEAD)}

    def check_state(self) -> checkpoint.SearchState:
        expected = self.runtime.lexical_status(self.actor)
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            state = checkpoint.search_state(db, lambda: None)
            assert state is not None
            self.assertEqual(state.status, expected)
            valid = {row[0] for row in db.execute(state.valid_query)}
            pending = {row[0] for row in db.execute(state.pending_query)}
            self.assertFalse(valid & pending)
            self.assertEqual(len(valid), expected.indexed_messages)
            self.assertEqual(len(pending), expected.pending_messages)
            return state

    def test_default_off_no_automatic_ddl_and_explicit_id_dependency(self) -> None:
        self.assertFalse(self.runtime.lexical_checkpoint_enabled(self.actor))
        MemoryRuntime(self.config).search(self.actor, "紫罗兰")
        self.assertEqual(self.objects(), [])
        with self.assertRaisesRegex(MemoryFailure, "lexical_checkpoint_requires_id_acceleration"):
            self.runtime.configure_lexical_checkpoint(self.actor, enabled=True)
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.actor))
        self.assertFalse(self.runtime.configure_lexical_checkpoint(self.actor, enabled=False))
        self.assertEqual(self.objects(), [])

    def test_lifecycle_preserves_originals_and_index_and_reopens(self) -> None:
        self.prepare()
        expected = self.snapshot()
        self.assertEqual(len(self.objects()), 16)
        self.assertTrue(self.runtime.configure_lexical_checkpoint(self.actor, enabled=True))
        reopened = MemoryRuntime(self.config)
        self.assertTrue(reopened.lexical_checkpoint_enabled(self.actor))
        self.check_state()
        self.assertEqual(self.runtime.refresh_lexical_checkpoint(self.actor), 0)
        self.assertEqual(self.snapshot(), expected)
        self.assertFalse(reopened.configure_lexical_checkpoint(self.actor, enabled=False))
        self.assertEqual(self.objects(), [])
        with closing(sqlite3.connect(self.database)) as db:
            for table in ("messages", "lexical", "lexical_docs"):
                self.assertEqual(db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(), expected[table])
        restored = reopened.read(self.actor, self.receipt.message_id)
        assert restored is not None
        self.assertEqual(restored.event, self.event)

    def test_scopes_strict_types_and_limit_do_not_create_objects(self) -> None:
        ordinary = pair_client(self.config, client_id="ordinary", entry="ordinary",
            access_path=self.config.parent / "mcp-access-ordinary.json", scopes=("capture", "recall"))
        for enabled in (True, False):
            with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                self.runtime.configure_lexical_checkpoint(ordinary, enabled=enabled)
        with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
            self.runtime.refresh_lexical_checkpoint(ordinary)
        value: Any
        for value in (1, "true", None):
            with self.assertRaisesRegex(MemoryFailure, "invalid_lexical_checkpoint"):
                self.runtime.configure_lexical_checkpoint(self.actor, enabled=value)
        for value in (True, 0, 5001, 1.5):
            with self.assertRaisesRegex(MemoryFailure, "invalid_page"):
                self.runtime.refresh_lexical_checkpoint(self.actor, limit=value)
        self.assertEqual(self.objects(), [])

    def test_new_capture_stale_normalization_loss_duplicates_and_suppression(self) -> None:
        self.prepare()
        self.check_state()
        added = self.runtime.submit(self.actor, replace(self.event, source=replace(self.event.source, event="new"), sequence=2))
        self.check_state()
        self.runtime.refresh_lexical_window(self.actor)
        self.check_state()
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("UPDATE lexical_docs SET normalization_version=0 WHERE message_id=?", (self.receipt.message_id,))
        self.check_state()
        self.runtime.refresh_lexical_window(self.actor)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("DELETE FROM lexical WHERE message_id=?", (added.message_id,))
        self.check_state()
        self.runtime.refresh_lexical_window(self.actor)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("INSERT INTO lexical SELECT message_id,terms FROM lexical WHERE message_id=?", (self.receipt.message_id,))
        self.check_state()
        self.runtime.change(self.actor, Change("hide", self.receipt.message_id, 1, "suppress", added.message_id, "synthetic"))
        self.check_state()
        with closing(sqlite3.connect(self.database)) as db:
            dirty = db.execute(f"SELECT count(*) FROM {checkpoint._DIRTY}").fetchone()[0]
        self.assertEqual(self.runtime.refresh_lexical_checkpoint(self.actor, limit=1), 1)
        self.check_state()
        self.assertEqual(self.runtime.refresh_lexical_checkpoint(self.actor), dirty - 1)
        self.check_state()

    def test_missing_and_modified_ownership_or_metadata_fall_back_without_repair(self) -> None:
        self.prepare()
        for sql in (
            f"UPDATE {checkpoint._HEAD} SET normalization_version=99",
            f"UPDATE {checkpoint._HEAD} SET source_count=source_count+1",
            f"DELETE FROM {checkpoint._ROWS}",
            f"DROP TRIGGER {checkpoint._PREFIX}track_messages_insert",
            f"DROP TABLE {checkpoint._HEAD}",
        ):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                    db.execute(sql)
                before = self.objects()
                self.assertFalse(self.runtime.lexical_checkpoint_enabled(self.actor))
                with self.runtime._connection() as db:
                    db.execute("BEGIN")
                    self.assertIsNone(checkpoint.search_state(db, lambda: None))
                self.assertEqual(self.objects(), before)
                self.assertTrue(self.runtime.configure_lexical_checkpoint(self.actor, enabled=True))
                self.check_state()

    def test_conflicting_case_object_is_never_overwritten(self) -> None:
        name = checkpoint._HEAD.upper()
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute(f"CREATE TABLE {name}(keep TEXT)")
            db.execute(f"INSERT INTO {name} VALUES ('synthetic marker')")
        before = self.objects()
        for enabled in (True, False):
            with self.assertRaisesRegex(MemoryFailure, "lexical_checkpoint_conflict"):
                self.runtime.configure_lexical_checkpoint(self.actor, enabled=enabled)
        self.assertFalse(self.runtime.lexical_checkpoint_enabled(self.actor))
        self.assertEqual(self.objects(), before)

    def test_queue_storage_failure_rolls_back_raw_then_removal_allows_idempotent_retry(self) -> None:
        self.prepare()
        event = replace(self.event, source=replace(self.event.source, event="queue-failure"), sequence=2)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute(f"DROP TABLE {checkpoint._DIRTY}")
        before = self.runtime.status(self.actor).messages
        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
            self.runtime.submit(self.actor, event)
        self.assertEqual(self.runtime.status(self.actor).messages, before)
        self.assertFalse(self.runtime.configure_lexical_checkpoint(self.actor, enabled=False))
        receipt = self.runtime.submit(self.actor, event)
        self.assertEqual(self.runtime.submit(self.actor, event), receipt)
        self.assertEqual(self.runtime.status(self.actor).messages, before + 1)

    def test_reconcile_failure_after_queue_delete_rolls_back_every_checkpoint_row(self) -> None:
        self.prepare()
        self.runtime.submit(self.actor, replace(self.event, source=replace(self.event.source, event="new"), sequence=2))
        before = self.snapshot()
        real_connect = sqlite3.connect
        class Fail(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                result = super().execute(sql, parameters)
                if sql.startswith(f"DELETE FROM {checkpoint._DIRTY}"):
                    raise sqlite3.OperationalError("synthetic reconcile failure")
                return result
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Fail)
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                self.runtime.refresh_lexical_checkpoint(self.actor)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runtime.refresh_lexical_checkpoint(self.actor), 1)
        self.check_state()

    def test_cancel_does_not_turn_into_full_result_or_leave_persistent_changes(self) -> None:
        self.prepare()
        before = self.snapshot()
        def canceled() -> None:
            raise MemoryFailure("search_canceled")
        with self.runtime._connection() as db:
            db.execute("BEGIN")
            with self.assertRaisesRegex(MemoryFailure, "search_canceled"):
                checkpoint.search_state(db, canceled)
        self.assertEqual(self.snapshot(), before)
        self.check_state()

    def test_large_lookahead_offset_late_match_and_false_positive_fall_back_exactly(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.actor, enabled=True)
        noise = replace(self.event, text="甲乙甲丙乙甲乙", occurred_at="2020-01-01T00:00:00Z")
        for number in range(300):
            self.runtime.submit(self.actor, replace(noise, source=replace(noise.source, event=f"noise-{number}"), sequence=number+10))
        late = self.runtime.submit(self.actor, replace(noise, source=replace(noise.source, event="late"), sequence=400,
            text="甲乙甲乙", occurred_at="2022-01-01T00:00:00Z"))
        self.runtime.refresh_lexical_window(self.actor)
        self.runtime.configure_lexical_checkpoint(self.actor, enabled=True)
        for query, limit, offset, fast in (("甲", 10, 0, True), ("甲", 2, 3, True),
                ("甲乙甲乙", 10, 0, False), ("甲乙甲乙甲", 10, 0, False)):
            with self.subTest(query=query, offset=offset):
                with patch.object(checkpoint, "search_state", return_value=None):
                    expected = self.runtime.search(self.actor, query, limit=limit, offset=offset)
                with patch.object(implementation, "_LEXICAL_QUERY_CANDIDATE_LIMIT", 2), \
                        patch.object(self.runtime, "_search_hits", wraps=self.runtime._search_hits) as calls:
                    actual = self.runtime.search(self.actor, query, limit=limit, offset=offset)
                self.assertEqual(actual, expected)
                sql = [call.args[1] for call in calls.call_args_list]
                self.assertTrue(sql[0].startswith("WITH evernook_checkpoint_ordered"))
                self.assertEqual(len(sql), 1 if fast else 2)
                if query == "甲乙甲乙":
                    self.assertEqual([hit.message_id for hit in actual.hits], [late.message_id])
                if query == "甲乙甲乙甲":
                    self.assertEqual(actual.hits, ())

    def test_query_keeps_snapshot_when_peer_capture_commits_after_checkpoint_read(self) -> None:
        self.prepare()
        prior = self.runtime.search(self.actor, "合成")
        real_state = checkpoint.search_state
        submitted: list[Receipt] = []
        def peer(db: sqlite3.Connection, check: Any) -> checkpoint.SearchState | None:
            state = real_state(db, check)
            if not submitted:
                submitted.append(self.runtime.submit(self.actor, replace(self.event,
                    source=replace(self.event.source, event="peer"), sequence=2)))
            return state
        with patch.object(checkpoint, "search_state", side_effect=peer):
            self.assertEqual(self.runtime.search(self.actor, "合成"), prior)
        after = self.runtime.search(self.actor, "合成")
        self.assertEqual(after.index.source_messages, 2)
        self.assertEqual(after.index.pending_messages, 1)
        self.assertEqual(len(after.hits), 2)

    def test_query_over_dirty_limit_and_missing_head_use_complete_original_route(self) -> None:
        self.prepare()
        for number in range(101):
            self.runtime.submit(self.actor, replace(self.event, source=replace(self.event.source, event=f"other-{number}"), sequence=number+2))
        with patch.object(checkpoint, "search_state", return_value=None):
            expected = self.runtime.search(self.actor, "合成", limit=100)
        self.assertEqual(self.runtime.search(self.actor, "合成", limit=100), expected)
        self.assertEqual(expected.index.source_messages, 102)
        self.runtime.refresh_lexical_checkpoint(self.actor)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute(f"DELETE FROM {checkpoint._HEAD}")
        self.assertEqual(self.runtime.search(self.actor, "合成", limit=100), expected)

    def test_corrupt_derived_watermark_and_blob_dirty_id_use_original_full_page(self) -> None:
        self.prepare()
        with patch.object(checkpoint, "search_state", return_value=None):
            expected = self.runtime.search(self.actor, "紫罗兰")
        for sql, parameters in (
                (f"UPDATE {checkpoint._ROWS} SET received='synthetic invalid watermark'", ()),
                (f"INSERT INTO {checkpoint._DIRTY} VALUES (?)", (b"synthetic invalid id",))):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                    db.execute(sql, parameters)
                self.assertEqual(self.runtime.search(self.actor, "紫罗兰"), expected)
                self.assertFalse(self.runtime.lexical_checkpoint_enabled(self.actor))
                self.assertTrue(self.runtime.configure_lexical_checkpoint(self.actor, enabled=True))
                self.check_state()

    def test_fk_child_metadata_failure_blocks_opted_in_raw_write_without_replay(self) -> None:
        self.prepare()
        before = self.snapshot()
        real_connect = sqlite3.connect
        observed = []
        def deny_child(action: int, table: str | None, column: str | None, database: str | None, trigger: str | None) -> int:
            if action == sqlite3.SQLITE_READ and table == "lexical_docs":
                observed.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def unavailable_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            db: sqlite3.Connection = real_connect(*args, **kwargs)
            db.set_authorizer(deny_child)
            return db
        event = replace(self.event, source=replace(self.event.source, event="metadata-failure"), sequence=2)
        with patch("sqlite3.connect", unavailable_connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                self.runtime.submit(self.actor, event)
        self.assertTrue(observed)
        self.assertEqual(self.snapshot(), before)
        self.runtime.configure_lexical_checkpoint(self.actor, enabled=False)
        receipt = self.runtime.submit(self.actor, event)
        self.assertEqual(self.runtime.submit(self.actor, event), receipt)

    def test_existing_trusted_index_maintenance_reconciles_enabled_checkpoint(self) -> None:
        self.prepare()
        self.runtime.submit(self.actor, replace(self.event, source=replace(self.event.source, event="maintenance"), sequence=2))
        for limit in (1, 5000):
            self.runtime.refresh_lexical_window(self.actor, limit=limit)
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute(f"SELECT count(*) FROM {checkpoint._DIRTY}").fetchone()[0], 0)
            self.check_state()

    def test_build_commit_failure_leaves_no_checkpoint_or_raw_changes(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.actor, enabled=True)
        real_connect = sqlite3.connect
        class FailCommit(sqlite3.Connection):
            armed = False
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql == "COMMIT" and self.armed:
                    raise sqlite3.OperationalError("synthetic checkpoint commit failure")
                result = super().execute(sql, parameters)
                if sql.startswith(f"CREATE TABLE {checkpoint._ROWS}"):
                    self.armed = True
                return result
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=FailCommit)
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                self.runtime.configure_lexical_checkpoint(self.actor, enabled=True)
        self.assertEqual(self.objects(), [])
        self.assertEqual(self.runtime.status(self.actor).messages, 1)
        restored = self.runtime.read(self.actor, self.receipt.message_id)
        assert restored is not None
        self.assertEqual(restored.event, self.event)
        self.assertTrue(self.runtime.configure_lexical_checkpoint(self.actor, enabled=True))

    def test_sql_failure_can_fall_back_but_lost_snapshot_must_fail(self) -> None:
        self.prepare()
        expected = self.runtime.search(self.actor, "紫罗兰")
        real_connect = sqlite3.connect
        for lose_snapshot in (False, True):
            with self.subTest(lose_snapshot=lose_snapshot):
                observed = []
                class Fail(sqlite3.Connection):
                    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                        if sql.startswith("SELECT count(*),coalesce(sum(indexed),0),coalesce(sum"):
                            observed.append(True)
                            if lose_snapshot:
                                super().execute("ROLLBACK")
                            raise sqlite3.OperationalError("synthetic checkpoint scan failure")
                        return super().execute(sql, parameters)
                def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                    return real_connect(*args, **kwargs, factory=Fail)
                with patch("sqlite3.connect", side_effect=connect):
                    if lose_snapshot:
                        with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                            self.runtime.search(self.actor, "紫罗兰")
                    else:
                        self.assertEqual(self.runtime.search(self.actor, "紫罗兰"), expected)
                self.assertTrue(observed)
                self.assertEqual(self.runtime.search(self.actor, "紫罗兰"), expected)

    def prepare_purge(self) -> tuple[PurgeWorkflow, PurgeDecision]:
        self.prepare()
        workflow = PurgeWorkflow(self.runtime)
        preview = workflow.preview(self.actor, "checkpoint-purge", (self.receipt.message_id,))
        confirmation = self.runtime.submit(self.actor, replace(self.event,
            source=replace(self.event.source, event="purge-confirmation"), sequence=2,
            text="合成用户确认彻底删除预览中的唯一来源。"))
        self.runtime.refresh_lexical_window(self.actor)
        self.runtime.configure_lexical_checkpoint(self.actor, enabled=True)
        return workflow, PurgeDecision(preview.request_id, confirmation.message_id)

    def test_confirmed_purge_removes_checkpoint_metadata_before_completion(self) -> None:
        workflow, decision = self.prepare_purge()
        result = workflow.purge(self.actor, decision)
        self.assertEqual(result.state, "completed")
        self.assertEqual(workflow.purge(self.actor, decision), result)
        with closing(sqlite3.connect(self.database)) as db:
            for table in (checkpoint._ROWS, checkpoint._DIRTY):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE message_id=?", (self.receipt.message_id,)).fetchone())
        self.assertIsNone(self.runtime.read(self.actor, self.receipt.message_id))
        with self.assertRaisesRegex(MemoryFailure, "source_purged"):
            self.runtime.submit(self.actor, self.event)
        self.assertTrue(self.runtime.lexical_checkpoint_enabled(self.actor))
        self.check_state()

    def test_purge_checkpoint_failure_retains_blocked_sources_and_retry_completes(self) -> None:
        workflow, decision = self.prepare_purge()
        real_connect = sqlite3.connect
        class Fail(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                result = super().execute(sql, parameters)
                if sql.startswith(f"DELETE FROM {checkpoint._ROWS} WHERE message_id IN"):
                    raise sqlite3.OperationalError("synthetic checkpoint purge failure")
                return result
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Fail)
        with patch("sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                workflow.purge(self.actor, decision)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM messages WHERE id=?", (self.receipt.message_id,)).fetchone())
            self.assertIsNotNone(db.execute(f"SELECT 1 FROM {checkpoint._ROWS} WHERE message_id=?", (self.receipt.message_id,)).fetchone())
            self.assertEqual(db.execute("SELECT state FROM purge_requests_v1 WHERE request_id=?", (decision.request_id,)).fetchone()[0], "blocked")
        self.assertIsNone(self.runtime.read(self.actor, self.receipt.message_id))
        self.assertEqual(workflow.purge(self.actor, decision).state, "completed")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(db.execute(f"SELECT 1 FROM {checkpoint._ROWS} WHERE message_id=?", (self.receipt.message_id,)).fetchone())
        self.check_state()

    def test_archive_snapshot_omits_checkpoint_without_disabling_live_tracking(self) -> None:
        from evernook import restore_archive
        directory = self.config.parent.parent / "archive-fixture"
        directory.mkdir()
        config, actor = initialize(directory / "runtime", client_id="synthetic", entry="test", allow_archive_review=True)
        runtime = MemoryRuntime(config)
        saved = runtime.submit(actor, self.event)
        runtime.configure_lexical_id_acceleration(actor, enabled=True)
        runtime.refresh_lexical_window(actor)
        runtime.configure_lexical_checkpoint(actor, enabled=True)
        exported = runtime.export_archive(actor, directory / "export")
        self.assertEqual(exported.messages, 1)
        archived = restore_archive(directory / "export", directory / "review")
        self.assertEqual(archived.review_message(saved.message_id).message.event, self.event)
        self.assertEqual(archived.status().recovery_state, "review_required")
        self.assertTrue(runtime.lexical_checkpoint_enabled(actor))
        with closing(sqlite3.connect(directory / "export/evidence.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall(), [])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE lower(name) GLOB ?", (checkpoint._PREFIX + "*",)).fetchall(), [])

    def test_archive_rejects_unknown_trigger_attached_to_checkpoint_rows(self) -> None:
        directory = self.config.parent.parent / "archive-untrusted-fixture"
        directory.mkdir()
        config, actor = initialize(directory / "runtime", client_id="synthetic", entry="test", allow_archive_review=True)
        runtime = MemoryRuntime(config)
        runtime.submit(actor, self.event)
        runtime.configure_lexical_id_acceleration(actor, enabled=True)
        runtime.configure_lexical_checkpoint(actor, enabled=True)
        with closing(sqlite3.connect(config.parent / "memory.sqlite3", isolation_level=None)) as db:
            db.execute(f"CREATE TRIGGER synthetic_unknown BEFORE DELETE ON {checkpoint._ROWS} BEGIN SELECT 1;END")
        with self.assertRaisesRegex(MemoryFailure, "archive_integrity_mismatch"):
            runtime.export_archive(actor, directory / "export")
        self.assertFalse((directory / "export").exists())
        with closing(sqlite3.connect(config.parent / "memory.sqlite3")) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_unknown'").fetchone())


if __name__ == "__main__":
    unittest.main()
