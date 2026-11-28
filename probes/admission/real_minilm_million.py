"""Build a resumable, production-space MiniLM fixture from the frozen synthetic million."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import time

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall
from evernook import embedding
from evernook.embedding import unit_vector
from evernook.paths import external
from evernook.runtime import _json
from evernook.semantic import _ELIGIBLE
from probes.admission.synthetic_full_vector_scale import COUNT, POLICY, TOPICS, copy_runtime


def digest(path: Path) -> str:
    with path.open("rb") as asset:
        return hashlib.file_digest(asset, "sha256").hexdigest()


def frozen_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_bytes())
    if manifest["expected_messages"] != COUNT or manifest["policy_sha256"] != digest(POLICY):
        raise ValueError("not the frozen synthetic million")
    return manifest


def clone(source: Path, root: Path) -> None:
    manifest = frozen_manifest(source / "manifest.json")
    root.mkdir()
    copy_runtime(source / "runtime", root / "runtime")
    (root / "source-manifest.json").write_text(_json(manifest), encoding="utf-8")


class BatchWorker:
    """One unmodified offline production worker. Assets are verified at both build boundaries."""

    def __init__(self, backend: LocalMiniLM, stack: ExitStack):
        self.backend = backend
        self.process = stack.enter_context(subprocess.Popen(
            [str(backend.node), str(Path(embedding.__file__).with_name("embedding_worker.mjs")), "--session"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW))
        stack.callback(self.close)
        self.sequence = 0

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        if self.process.wait(timeout=15) != 0:
            raise RuntimeError("owned model worker failed")

    def infer(self, texts: list[str]) -> list[bytes]:
        result = []
        for start in range(0, len(texts), 16):
            batch = tuple(texts[start:start + 16])
            self.backend._validate_request(batch, 120)
            self.sequence += 1
            request = {"id": self.sequence, "model_dir": str(self.backend.model_dir),
                       "transformers_dir": str(self.backend.transformers_dir), "texts": batch}
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.write(_json(request).encode() + b"\n")
            self.process.stdin.flush()
            chunks = self.backend._decode(self.process.stdout.readline(1048577), len(batch), request_id=self.sequence)
            for vector in chunks:
                if vector is None:
                    raise ValueError("unexpected token overflow")
                result.append(struct.pack("<384f", *unit_vector(vector, 384)))
        return result


def expected_text(index: int, first: str) -> str:
    return first if index == 0 else f"合成条目{index:07d}：{TOPICS[index % 8]}；保留数字37.50与🙂。"


def build(root: Path, config: Path, workers: int, stop_after: int) -> None:
    frozen_manifest(root / "source-manifest.json")
    backend = LocalMiniLM.from_file(config)
    first = json.loads(POLICY.read_bytes())["scale"]["pure_semantic_original"]
    backend._request((first,), lambda: 120)
    key = backend.space.key
    started = time.monotonic()
    local = MemoryRuntime(root / "runtime/runtime.json")
    with closing(sqlite3.connect(local.root / "memory.sqlite3")) as db, ExitStack() as stack:
        if db.execute("SELECT count(*),min(rowid),max(rowid) FROM messages").fetchone() != (COUNT, 1, COUNT):
            raise ValueError("source inventory mismatch")
        if db.execute("SELECT specification FROM semantic_spaces_v1 WHERE id=?", (key,)).fetchone() != (_json(asdict(backend.space)),):
            raise ValueError("source model mismatch")
        db.execute("CREATE TABLE IF NOT EXISTS scale_build_receipt(first INTEGER PRIMARY KEY,last INTEGER NOT NULL,sha256 TEXT NOT NULL)")
        cursor = db.execute("SELECT coalesce(max(last),0) FROM scale_build_receipt").fetchone()[0]
        pool = [BatchWorker(backend, stack) for _ in range(workers)]
        threads = stack.enter_context(ThreadPoolExecutor(workers))
        rows_per_worker = 128
        while cursor < stop_after:
            stop = min(cursor + workers * rows_per_worker, stop_after)
            rows = db.execute(f"""SELECT messages.rowid,messages.id,event,receipt,coalesce(v.version,1)
                FROM messages LEFT JOIN visibility v ON v.message_id=messages.id
                WHERE messages.rowid>? AND messages.rowid<=? AND {_ELIGIBLE} ORDER BY messages.rowid""",
                (cursor, stop)).fetchall()
            documents, texts = [], []
            for offset, (rowid, mid, event_raw, receipt_raw, state) in enumerate(rows):
                event, receipt = json.loads(event_raw), json.loads(receipt_raw)
                index = cursor + offset
                expected = expected_text(index, first)
                if (rowid != index + 1 or event["sequence"] != index or event["text"] != expected
                    or event["source"] != {"entry": "synthetic-scale", "session": "zh-scale-v1", "branch": "main", "event": f"m{index:07d}"}
                    or event["role"] != "user" or event["completion"] != "complete" or receipt["message_id"] != mid):
                    raise ValueError("source text, identity or order mismatch")
                documents.append((key, mid, state, receipt["content_version"], len(expected), 1))
                texts.append(expected)
            if len(texts) != stop - cursor:
                raise ValueError("missing eligible source")
            jobs = [threads.submit(worker.infer, texts[i:i + rows_per_worker])
                    for worker, i in zip(pool, range(0, len(texts), rows_per_worker))]
            vectors = itertools.chain.from_iterable(job.result() for job in jobs)
            chunks = [(k, mid, 0, size, state, version, vector)
                      for (k, mid, state, version, size, _), vector in zip(documents, vectors, strict=True)]
            receipt_digest = hashlib.sha256()
            for row in chunks:
                receipt_digest.update(row[1].encode() + row[-1])
            with db:
                db.executemany("INSERT OR REPLACE INTO semantic_documents_v1 VALUES (?,?,?,?,?,?)", documents)
                db.executemany("INSERT OR REPLACE INTO semantic_chunks_v1 VALUES (?,?,?,?,?,?,?)", chunks)
                db.execute("INSERT INTO scale_build_receipt VALUES (?,?,?)", (cursor + 1, stop, receipt_digest.hexdigest()))
            if cursor // 8192 != stop // 8192 or stop == stop_after:
                print(_json({"committed": stop, "window_seconds": time.monotonic() - started}), flush=True)
            cursor = stop
    backend._request((first,), lambda: 120)
    if stop_after < COUNT:
        return
    with closing(sqlite3.connect(local.root / "memory.sqlite3")) as db:
        receipts = db.execute("SELECT first,last,sha256 FROM scale_build_receipt ORDER BY first").fetchall()
        offset = 0
        combined = hashlib.sha256()
        for a, b, h in receipts:
            if a != offset + 1:
                raise ValueError("checkpoint gap")
            offset = b
            combined.update(bytes.fromhex(h))
        if offset != COUNT:
            raise ValueError("incomplete inference")
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise ValueError("checkpoint busy")
    reader = local.access_from_file(local.root / "mcp-access-scale-writer.json")
    maintainer = local.access_from_file(local.root / "mcp-access-scale-maintenance.json")
    recall = SemanticRecall(local, backend)
    recall.configure_candidate_index(maintainer, enabled=True)
    recall.configure_coverage_checkpoint(maintainer, enabled=True)
    status = recall.status(reader)
    if (status.state != "ready" or (status.source_messages, status.indexed_messages, status.stored_vectors,
            status.failed_chunks, status.pending_messages) != (COUNT, COUNT, COUNT, 0, 0)):
        raise ValueError(f"public coverage mismatch {status}")
    report = {"public_status": asdict(status), "space": asdict(backend.space),
              "inference_batch_receipts_sha256": combined.hexdigest(), "workers": workers,
              "last_window_seconds": time.monotonic() - started, "build_probe_sha256": digest(Path(__file__)),
              "node_worker_sha256": digest(Path(embedding.__file__).with_name("embedding_worker.mjs"))}
    (root / "production-model-prepared.json").write_text(_json(report), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("clone", "build"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--stop-after", type=int, default=COUNT)
    args = parser.parse_args()
    if args.action == "clone" and args.source:
        clone(external(args.source), external(args.root))
    elif args.action == "build" and args.config and 0 < args.stop_after <= COUNT:
        build(external(args.root), external(args.config), args.workers, args.stop_after)
    else:
        parser.error("clone requires --source; build requires --config and a valid stop limit")
