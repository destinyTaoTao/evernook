"""Reconcile the isolated production-model build receipts with the persisted million."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall
from evernook import semantic_checkpoint
from evernook.embedding import unit_vector
from evernook.paths import external
from probes.admission.real_minilm_million import frozen_manifest
from probes.admission.synthetic_full_vector_scale import COUNT


def verify(root: Path, config: Path) -> None:
    frozen_manifest(root / "source-manifest.json")
    prepared = json.loads((root / "production-model-prepared.json").read_bytes())
    model = LocalMiniLM.from_file(config)
    if prepared["space"] != asdict(model.space):
        raise ValueError("wrong model")
    runtime = MemoryRuntime(root / "runtime/runtime.json")
    actor = runtime.access_from_file(runtime.root / "mcp-access-scale-writer.json")
    maint = runtime.access_from_file(runtime.root / "mcp-access-scale-maintenance.json")
    recall = SemanticRecall(runtime, model)
    started = time.monotonic()
    path = runtime.root / "memory.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        batches = db.execute("SELECT first,last,sha256 FROM scale_build_receipt ORDER BY first").fetchall()
        rows = iter(db.execute("""SELECT m.rowid,m.id,c.vector FROM messages m
            CROSS JOIN semantic_chunks_v1 c ON c.message_id=m.id AND c.space_id=? AND c.start=0
            ORDER BY m.rowid""", (model.space.key,)))
        combined, inventory = hashlib.sha256(), hashlib.sha256()
        processed = 0
        for first, last, expected in batches:
            if first != processed + 1:
                raise ValueError("receipt gap")
            digest = hashlib.sha256()
            for pos in range(first, last + 1):
                rowid, mid, raw = next(rows)
                if rowid != pos or len(raw) != model.space.dimensions * 4:
                    raise ValueError("missing or malformed vector")
                digest.update(mid.encode() + raw)
                inventory.update(mid.encode() + raw)
            if digest.hexdigest() != expected:
                raise ValueError("persisted input and vector differ from inference receipt")
            processed = last
            combined.update(digest.digest())
        if (processed != COUNT or next(rows, None) is not None
                or combined.hexdigest() != prepared["inference_batch_receipts_sha256"]):
            raise ValueError("incomplete vector coverage")
        # Match the same batch composition because the graph uses dynamic quantization.
        independent_batches = []
        for first in (1, 16385, 492033, 999985):
            sample = db.execute("""SELECT json_extract(m.event,'$.text'),c.vector FROM messages m
                CROSS JOIN semantic_chunks_v1 c ON c.message_id=m.id AND c.space_id=? AND c.start=0
                WHERE m.rowid BETWEEN ? AND ? ORDER BY m.rowid""", (model.space.key, first, first + 15)).fetchall()
            inferred = model.embed(tuple(text for text, _ in sample), timeout=120)
            if len(sample) != 16 or not all(vector is not None and
                    struct.pack("<384f", *unit_vector(vector, 384)) == raw
                    for vector, (_, raw) in zip(inferred, sample, strict=True)):
                raise ValueError(f"independent public model vector mismatch at {first}")
            independent_batches.append(first)
    readback_seconds = time.monotonic() - started
    # SQLite backup can change schema_version. Rebuild using the public APIs in
    # dependency order so lexical and semantic checkpoints both bind to the current schema.
    before = runtime.lexical_checkpoint_enabled(actor)
    runtime.configure_lexical_checkpoint(maint, enabled=True)
    recall.configure_coverage_checkpoint(maint, enabled=True)
    lexical = runtime.lexical_status(actor)
    semantic = recall.status(actor)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        semantic_current = semantic_checkpoint.status(db, model.space.key) is not None
    flags = {"lexical_checkpoint": runtime.lexical_checkpoint_enabled(actor),
             "candidate_index": recall.candidate_index_enabled(actor),
             "semantic_checkpoint": semantic_current,
             "lexical_id_acceleration": runtime.lexical_id_acceleration_enabled(actor)}
    if (not all(flags.values()) or lexical.state != "ready" or lexical.indexed_messages != COUNT
            or semantic.state != "ready" or semantic.indexed_messages != COUNT or semantic.stored_vectors != COUNT
            or semantic.failed_chunks or semantic.pending_messages):
        raise ValueError("public index or checkpoint unavailable")
    result = {"source_and_vector_inventory_sha256": inventory.hexdigest(), "readback_verified": processed,
              "matched_public_model_batches_start_rowid": independent_batches,
              "matched_recomputed_f32_vectors": 16 * len(independent_batches),
              "full_readback_and_sample_seconds": readback_seconds, "seconds": time.monotonic() - started,
              "lexical_checkpoint_before_verification": before, "effective_indexes": flags,
              "lexical": asdict(lexical), "semantic": asdict(semantic), "sqlite_version": sqlite3.sqlite_version}
    with (root / "production-model-verified.json").open("x", encoding="utf-8") as file:
        json.dump(result, file, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    verify(external(args.root), external(args.config))
