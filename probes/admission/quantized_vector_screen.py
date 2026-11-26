"""Compare binary/int8 shortlist recall with exact float32 on a read-only synthetic vector fixture."""
from __future__ import annotations

import argparse
from contextlib import closing
import importlib
import json
import math
from pathlib import Path
import random
import sqlite3
import struct
import time


def screen(path: Path, *, queries: int, shortlist: int, top: int) -> dict[str, object]:
    if not 1 <= top <= shortlist <= 10000 or not 1 <= queries <= 32:
        raise ValueError("invalid screen size")
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        db.enable_load_extension(True)
        try:
            importlib.import_module("sqlite_vec").load(db)
        finally:
            db.enable_load_extension(False)
        if db.execute("SELECT vec_version()").fetchone()[0] != "v0.1.9":
            raise ValueError("expected sqlite-vec 0.1.9")
        db.execute("BEGIN")
        largest = db.execute("""SELECT space_id FROM semantic_chunks_v1
            GROUP BY space_id ORDER BY count(*) DESC,space_id LIMIT 1""").fetchone()
        if largest is None:
            raise ValueError("empty synthetic fixture")
        space = largest[0]
        # Only temporary SQLite storage receives derived vectors. Row IDs are
        # local to the source snapshot, held until all exact reranking is done.
        started = time.perf_counter()
        db.execute("""CREATE TEMP TABLE coarse AS SELECT rowid AS source_row,
            vec_quantize_binary(vector) AS bits, vec_quantize_int8(vector,'unit') AS bytes
            FROM semantic_chunks_v1 WHERE space_id=? ORDER BY rowid""", (space,))
        build_seconds = time.perf_counter() - started
        n, binary_size, int8_size = db.execute(
            "SELECT count(*),sum(length(bits)),sum(length(bytes)) FROM coarse").fetchone()
        if n < shortlist:
            raise ValueError("fixture smaller than requested shortlist")
        cases = []
        for i in range(queries):
            raw = db.execute("SELECT vector FROM semantic_chunks_v1 WHERE rowid=(SELECT source_row FROM coarse ORDER BY rowid LIMIT 1 OFFSET ?)",
                             (n * (2 * i + 1) // (2 * queries),)).fetchone()[0]
            values = struct.unpack(f"<{len(raw) // 4}f", raw)
            rng = random.Random(104729 + i)
            perturbed = [x + rng.uniform(-0.5, 0.5) / math.sqrt(len(values)) for x in values]
            norm = math.sqrt(sum(x * x for x in perturbed))
            query = struct.pack(f"<{len(values)}f", *(x / norm for x in perturbed))
            order = " ORDER BY vec_distance_cosine(vector,?),message_id,start LIMIT ?"
            started = time.perf_counter()
            truth = [r[0] for r in db.execute("SELECT rowid FROM semantic_chunks_v1 NOT INDEXED WHERE space_id=?" + order,
                                              (space, query, top))]
            case: dict[str, object] = {"exact_seconds": time.perf_counter() - started}
            for name, distance in (("binary", "vec_distance_hamming(vec_bit(bits),vec_quantize_binary(?))"),
                                   ("int8", "vec_distance_cosine(vec_int8(bytes),vec_quantize_int8(?,'unit'))")):
                started = time.perf_counter()
                rows = [r[0] for r in db.execute(f"""WITH shortlist AS MATERIALIZED (
                    SELECT source_row FROM coarse ORDER BY {distance},source_row LIMIT ?)
                    SELECT rowid FROM semantic_chunks_v1 WHERE rowid IN (SELECT source_row FROM shortlist)"""
                    + order, (query, shortlist, query, top))]
                case[name] = {"seconds": time.perf_counter() - started, "overlap": len(set(rows) & set(truth)),
                              "identical": rows == truth}
            cases.append(case)
        return {"chunks": n, "top": top, "shortlist": shortlist, "build_seconds": build_seconds,
                "binary_payload_bytes": binary_size, "int8_payload_bytes": int8_size, "queries": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("synthetic_database", type=Path)
    parser.add_argument("--queries", type=int, default=4)
    parser.add_argument("--shortlist", type=int, default=1000)
    parser.add_argument("--top", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(screen(args.synthetic_database, queries=args.queries, shortlist=args.shortlist, top=args.top)))
