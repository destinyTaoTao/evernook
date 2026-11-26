"""Measure ordered keyword candidates on a stationary copy of the synthetic million-row ledger."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from evernook.lexical import query_expression
from evernook.semantic import SemanticRecall


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--trace", action="store_true", help="Inspect SQL paths separately from latency measurements.")
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        parser.error("The fixture must be offline and checkpointed.")
    before = path.stat()
    cases = [
        ("common", ("合成", "成条", "条目"), None, None),
        ("mixed_unique", ("合成", "湿地", "白鹭"), None, None),
        ("mixed_topic", ("合成", "厨房", "库存"), None, None),
        ("unique", ("湿地", "白鹭"), None, None),
        ("absent", ("鲸鲨", "泥鳅"), None, None),
        ("one_day", ("合成", "厨房"), "2021-01-02T00:00:00+00:00", "2021-01-03T00:00:00+00:00"),
    ]
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        runs = []
        for label, words, since, until in cases:
            expression = " OR ".join("(" + query_expression((word,)) + ")" for word in words)
            sql: list[str] = []
            if args.trace:
                def trace(statement: str) -> None:
                    if not statement.startswith("--"):
                        sql.append(statement)
                db.set_trace_callback(trace)
            start = time.perf_counter()
            ids = SemanticRecall._keyword_ids(db, expression, since=since, until=until,
                                               current_input_id=None, check=lambda: 120.0)
            elapsed = time.perf_counter() - start
            db.set_trace_callback(None)
            runs.append({"query": label, "seconds": round(elapsed, 4), "count": len(ids),
                         "ordered_sha256": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                         "score_bound_used": any("AND bm25(lexical)<=" in s for s in sql) if args.trace else None,
                         "full_ranking": any("WITH ranked AS MATERIALIZED" in s for s in sql) if args.trace else None})
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or wal.exists() and wal.stat().st_size:
        raise RuntimeError("Fixture changed while the probe was running.")
    print(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), "results": runs}, indent=2))


if __name__ == "__main__":
    main()
