from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from uuid import uuid4

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
import test_lexical_maintenance
import test_lexical_window
import test_search


class IndexedIDSearchTests(test_search.SearchTests):
    initial_fts_present = True

    def setUp(self) -> None:
        super().setUp()
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)


class IndexedIDMaintenanceTests(test_lexical_maintenance.LexicalMaintenanceTests):
    def setUp(self) -> None:
        super().setUp()
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)


class IndexedIDWindowTests(test_lexical_window.LexicalWindowTests):
    def setUp(self) -> None:
        super().setUp()
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)


class LexicalIDAccelerationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-lexical-id-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name)/"data", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.event = Event(Source("test", "session", "main", "first"), "user", 1, None, "complete", "合成：乌龙茶37.50元。🙂")
        self.receipt = self.runtime.submit(self.access, self.event)
        self.runtime.refresh_lexical(self.access)
        self.database = self.config.parent/"memory.sqlite3"

    def snapshot(self) -> dict[str, list[Any]]:
        with closing(sqlite3.connect(self.database)) as db:
            return {table: db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}").fetchall()
                for table, columns, order in (("messages", "*", "rowid"), ("lexical", "rowid,*", "rowid"),
                    ("lexical_docs", "*", "message_id"))}

    def test_explicit_upgrade_reverse_and_reopen_preserve_rows_duplicates_page_and_version(self) -> None:
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("INSERT INTO lexical(rowid,message_id,terms) SELECT 987,message_id,terms FROM lexical LIMIT 1")
            version = db.execute("PRAGMA user_version").fetchone()[0]
        before = self.snapshot()
        page = self.runtime.search(self.access, "乌龙")
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
        for enabled in (True, True, False, False):
            self.assertEqual(self.runtime.configure_lexical_id_acceleration(self.access, enabled=enabled), enabled)
            self.runtime = MemoryRuntime(self.config)
            self.assertEqual(self.runtime.lexical_id_acceleration_enabled(self.access), enabled)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.runtime.search(self.access, "乌龙"), page)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], version)
            self.assertEqual(db.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_no_automatic_migration_and_missing_fts_disable_does_not_create_tables(self) -> None:
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("DROP TABLE lexical")
            db.execute("DROP TABLE lexical_docs")
        self.assertFalse(self.runtime.configure_lexical_id_acceleration(self.access, enabled=False))
        self.assertEqual(self.runtime.lexical_status(self.access).state, "not_built")
        MemoryRuntime(self.config).search(self.access, "乌龙")
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
        self.runtime.refresh_lexical(self.access)
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        self.runtime.rebuild_lexical(self.access)
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
        self.assertEqual(len(self.runtime.search(self.access, "乌龙").hits), 1)

    def test_scope_and_strict_boolean_guard_precede_any_ddl(self) -> None:
        ordinary = pair_client(self.config, client_id="ordinary", entry="ordinary",
            access_path=self.config.parent/"mcp-access-ordinary.json", scopes=("capture", "recall"))
        for enabled in (True, False):
            with self.assertRaisesRegex(MemoryFailure, "permission_denied"):
                self.runtime.configure_lexical_id_acceleration(ordinary, enabled=enabled)
        value: Any
        for value in (1, "true", None):
            with self.assertRaisesRegex(MemoryFailure, "invalid_lexical_id_acceleration"):
                self.runtime.configure_lexical_id_acceleration(self.access, enabled=value)
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(ordinary))

    def test_unknown_or_case_conflicting_fts_is_never_replaced(self) -> None:
        for definition in ("CREATE TABLE lexical(message_id,terms)",
                "CREATE VIRTUAL TABLE lexical USING fts5(message_id,terms, tokenize='porter')",
                "CREATE VIRTUAL TABLE LEXICAL USING fts5(message_id,terms)"):
            with self.subTest(definition=definition):
                with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                    db.execute("DROP TABLE lexical")
                    db.execute(definition)
                    db.execute("INSERT INTO lexical VALUES ('synthetic-conflict','keep this row')")
                self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
                for enabled in (True, False):
                    with self.assertRaisesRegex(MemoryFailure, "lexical_id_acceleration_conflict"):
                        self.runtime.configure_lexical_id_acceleration(self.access, enabled=enabled)
                with closing(sqlite3.connect(self.database)) as db:
                    self.assertEqual(db.execute("SELECT * FROM lexical").fetchall(), [("synthetic-conflict", "keep this row")])

    def test_build_name_collision_is_not_deleted(self) -> None:
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("CREATE TABLE EVERNOOK_LEXICAL_ID_BUILD_V1(keep)")
            db.execute("INSERT INTO EVERNOOK_LEXICAL_ID_BUILD_V1 VALUES ('retained')")
        before = self.snapshot()
        with self.assertRaisesRegex(MemoryFailure, "lexical_id_acceleration_conflict"):
            self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT * FROM EVERNOOK_LEXICAL_ID_BUILD_V1").fetchall(), [("retained",)])

    def test_copy_drop_rename_and_commit_failures_rollback_both_directions(self) -> None:
        real_connect = sqlite3.connect
        for enabled in (True, False):
            self.runtime.configure_lexical_id_acceleration(self.access, enabled=not enabled)
            before = self.snapshot()
            for stage in ("copy", "drop", "rename", "commit"):
                injected: list[str] = []
                class Fault(sqlite3.Connection):
                    changed = False
                    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                        copying = sql.startswith("INSERT INTO evernook_lexical_id_build_v1(")
                        if copying:
                            self.changed = True
                        fail = ((stage == "copy" and copying) or (stage == "drop" and sql == "DROP TABLE lexical")
                            or (stage == "rename" and sql.startswith("ALTER TABLE evernook_lexical_id_build_v1"))
                            or (stage == "commit" and self.changed and sql == "COMMIT"))
                        if fail and stage == "commit":
                            injected.append(stage)
                            raise sqlite3.OperationalError("synthetic failed commit")
                        cursor = super().execute(sql, parameters)
                        if fail:
                            injected.append(stage)
                            raise sqlite3.OperationalError("synthetic migration failure")
                        return cursor
                def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                    return real_connect(*args, **kwargs, factory=Fault)
                with self.subTest(enabled=enabled, stage=stage), patch("sqlite3.connect", side_effect=connect):
                    with self.assertRaisesRegex(MemoryFailure, "storage_unavailable"):
                        self.runtime.configure_lexical_id_acceleration(self.access, enabled=enabled)
                self.assertEqual(injected, [stage])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.runtime.lexical_id_acceleration_enabled(self.access), not enabled)
                with closing(sqlite3.connect(self.database)) as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'evernook_lexical_id_build_v1%'").fetchone())

    def test_old_wal_reader_snapshot_survives_upgrade(self) -> None:
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("BEGIN")
            old = db.execute("SELECT rowid,* FROM lexical").fetchall()
            self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
            self.assertEqual(db.execute("SELECT rowid,* FROM lexical").fetchall(), old)
            self.assertIn("UNINDEXED", db.execute("SELECT sql FROM sqlite_master WHERE name='lexical'").fetchone()[0])
            db.execute("COMMIT")
            self.assertEqual(db.execute("SELECT rowid,* FROM lexical WHERE message_id MATCH ?", ('"'+self.receipt.message_id+'"',)).fetchall(), old)

    def test_legacy_zero_match_does_not_leave_duplicate_rows_on_refresh(self) -> None:
        for enabled in (False, True, False):
            self.runtime.configure_lexical_id_acceleration(self.access, enabled=enabled)
            with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
                db.execute("INSERT INTO lexical(message_id,terms) SELECT message_id,terms FROM lexical LIMIT 1")
                db.execute("DELETE FROM lexical_docs")
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 1)

    def test_indexed_lookup_uses_bounded_vm_work_and_exact_id_filter(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        selected = self.receipt.message_id
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.executemany("INSERT INTO lexical VALUES (?,?)", [(str(uuid4()), "synthetic") for _ in range(10000)])
            db.execute("INSERT INTO lexical VALUES (?,?)", (selected + "-extra", "synthetic token prefix alias"))
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT receipt FROM messages WHERE id=?", (selected,)).fetchall()
            work: list[int] = []
            def progress() -> int:
                work.append(1)
                return 0
            db.set_progress_handler(progress, 100)
            found = self.runtime._lexical_rowids(db, rows)
            db.set_progress_handler(None, 0)
            self.assertEqual(found, {selected: [1]})
            self.assertLess(len(work), 50)

    def test_optional_match_failure_falls_back_without_missing_old_rows(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("DELETE FROM lexical_docs")
        real_connect = sqlite3.connect
        injected: list[bool] = []
        class Fault(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("SELECT rowid,message_id FROM lexical WHERE message_id MATCH"):
                    injected.append(True)
                    raise sqlite3.OperationalError("synthetic optional read failure")
                return super().execute(sql, parameters)
        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **kwargs, factory=Fault)
        with patch("sqlite3.connect", side_effect=connect):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        self.assertEqual(injected, [True])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 1)

    def test_peer_reverse_after_lookup_invalidates_rowids_before_write(self) -> None:
        self.runtime.configure_lexical_id_acceleration(self.access, enabled=True)
        with closing(sqlite3.connect(self.database, isolation_level=None)) as db:
            db.execute("DELETE FROM lexical_docs")
        original = MemoryRuntime._lexical_rowids
        def lookup(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, list[int]] | None:
            result = original(db, rows)
            self.runtime.configure_lexical_id_acceleration(self.access, enabled=False)
            return result
        with patch.object(MemoryRuntime, "_lexical_rowids", side_effect=lookup):
            self.assertEqual(self.runtime.refresh_lexical(self.access).state, "ready")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lexical").fetchone()[0], 1)
        self.assertFalse(self.runtime.lexical_id_acceleration_enabled(self.access))
