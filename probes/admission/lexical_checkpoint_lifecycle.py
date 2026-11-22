"""Exercise checkpoint changes on a new private copy of the million-row fixture."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

from evernook import Change, Event, MemoryRuntime, Source
from evernook import lexical_checkpoint
from evernook.paths import external
from probes.admission.recall_shared_scan import validate_fixture
from probes.admission.synthetic_full_vector_scale import COUNT, copy_runtime


def verify(source: Path, output: Path) -> None:
    source, output = external(source), external(output)
    validate_fixture(source, COUNT)
    output.mkdir()
    root = output / "runtime"
    copy_runtime(source, root)
    runtime = MemoryRuntime(root / "runtime.json")
    writer = runtime.access_from_file(root / "mcp-access-scale-writer.json")
    maintainer = runtime.access_from_file(root / "mcp-access-scale-maintenance.json")
    if not runtime.lexical_id_acceleration_enabled(maintainer):
        raise ValueError("fixture requires the indexed FTS message ID format")

    log = output / "lifecycle.jsonl"

    def record(row: dict[str, object]) -> None:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(row, ensure_ascii=True, sort_keys=True), flush=True)

    def prepare(stage: str) -> None:
        started = time.perf_counter()
        runtime.configure_lexical_checkpoint(maintainer, enabled=True)
        elapsed = time.perf_counter() - started
        if not runtime.lexical_checkpoint_enabled(maintainer):
            raise ValueError("checkpoint did not become effective")
        record({"stage": stage, "seconds": elapsed})

    def compare(stage: str, query: str, ids: tuple[str, ...], state: str, pending: int, *,
                uses_checkpoint: bool) -> None:
        with patch.object(runtime, "_search_index", wraps=runtime._search_index) as full:
            actual = runtime.search(writer, query, limit=10, timeout=120)
            fallback = bool(full.call_count)
        with patch.object(lexical_checkpoint, "search_state", return_value=None):
            authority = runtime.search(writer, query, limit=10, timeout=120)
        if (actual != authority or tuple(hit.message_id for hit in actual.hits) != ids
                or (actual.index.state, actual.index.pending_messages) != (state, pending)
                or (not fallback) != uses_checkpoint):
            raise ValueError(f"unexpected lifecycle result at {stage}")
        encoded = json.dumps(asdict(actual), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        record({"stage": stage, "coverage": asdict(actual.index), "used_checkpoint": uses_checkpoint,
                "hits": len(ids), "full_page_sha256": sha256(encoded).hexdigest()})

    def event(number: int, text: str) -> Event:
        return Event(Source("synthetic-scale", "checkpoint-lifecycle", "main", str(number)),
                     "user", number, "2026-11-03T00:00:00Z", "complete", text)

    prepare("prepare")
    initial = "合成甲雾枫回声"
    revised = "合成乙青芦灯阵"
    control = "合成丙校验说明"
    first = runtime.submit(writer, event(1, initial)).message_id
    compare("new_original_pending", initial, (first,), "behind", 1, uses_checkpoint=True)
    runtime.refresh_lexical_window(maintainer, limit=1)
    compare("new_original_indexed", initial, (first,), "ready", 0, uses_checkpoint=True)

    second = runtime.submit(writer, event(2, revised)).message_id
    runtime.change(maintainer, Change("checkpoint-life-correct", first, 1, "correct", second, "synthetic"))
    compare("correction_excludes_old", initial, (), "behind", 1, uses_checkpoint=True)
    compare("correction_includes_pending_replacement", revised, (second,), "behind", 1, uses_checkpoint=True)
    witness = runtime.submit(writer, event(3, control)).message_id
    runtime.refresh_lexical_window(maintainer, limit=2, transaction_size=2)
    runtime.change(maintainer, Change("checkpoint-life-suppress", second, 1, "suppress", witness, "synthetic"))
    compare("suppressed_after_indexing", revised, (), "ready", 0, uses_checkpoint=True)

    # Keep the index definition and edit only a copied FTS row to simulate lag.
    with closing(sqlite3.connect(root / "memory.sqlite3", isolation_level=None)) as db:
        db.execute("DELETE FROM lexical WHERE message_id=?", (witness,))
    compare("missing_fts_row", control, (witness,), "behind", 1, uses_checkpoint=True)
    runtime.refresh_lexical_window(maintainer, limit=1)
    compare("after_fts_repair", control, (witness,), "ready", 0, uses_checkpoint=True)

    dirty = tuple(event(number, f"合成检查点待处理队列{number:03d}尾") for number in range(4, 105))
    runtime.submit_many(writer, dirty[:100])
    last = runtime.submit(writer, dirty[-1]).message_id
    compare("dirty_queue_over_read_limit", dirty[-1].text, (last,), "behind", 101, uses_checkpoint=False)

    # For the overflow probe, query a unique late original as well.
    unique = "合成诊断唯一末项"
    late = runtime.submit(writer, event(105, unique)).message_id
    compare("dirty_overflow_preserves_late_original", unique, (late,), "behind", 102, uses_checkpoint=False)

    with closing(sqlite3.connect(root / "memory.sqlite3", isolation_level=None)) as db:
        db.execute("CREATE TABLE synthetic_checkpoint_schema_change(id INTEGER PRIMARY KEY)")
    if runtime.lexical_checkpoint_enabled(maintainer):
        raise ValueError("schema change did not invalidate checkpoint")
    compare("invalid_schema_fallback", unique, (late,), "behind", 102, uses_checkpoint=False)
    prepare("rebuild_after_schema_change")
    compare("rebuilt_with_pending_sources", unique, (late,), "behind", 102, uses_checkpoint=True)
    with closing(sqlite3.connect(root / "memory.sqlite3")) as db:
        if db.execute("SELECT count(*) FROM messages").fetchone()[0] != COUNT + 105:
            raise ValueError("synthetic originals were lost during checkpoint verification")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("fixture SQLite quick_check failed")
        if db.execute("SELECT 1 FROM messages WHERE id=?", (last,)).fetchone() is None:
            raise ValueError("overflow fixture original was lost")
    record({"stage": "physical_integrity", "messages": COUNT + 105, "quick_check": "ok"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing external synthetic million-row runtime")
    parser.add_argument("--output", type=Path, required=True, help="Fresh external evidence directory")
    args = parser.parse_args()
    verify(args.source, args.output)
