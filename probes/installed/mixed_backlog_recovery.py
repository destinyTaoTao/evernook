"""Follow the original mixed-drill receipts while only the installed service maintains the ledger."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
import time

from evernook import LocalMiniLM
from evernook.lexical import NORMALIZATION_IDENTITY
from evernook.service import ServiceClient
from probes.installed.mixed_service_drill import running_service


def inspect_batch(database, ids):
    digest = hashlib.sha256()
    lexical = vectors = 0
    session = None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        for i, mid in enumerate(ids):
            row = db.execute("""SELECT m.event,m.receipt,coalesce(v.version,1),coalesce(v.status,'active')
                FROM messages m LEFT JOIN visibility v ON v.message_id=m.id WHERE m.id=?""", (mid,)).fetchone()
            event, receipt = json.loads(row[0]), json.loads(row[1])
            source = event["source"]
            session = source["session"] if session is None else session
            if (not session.startswith("mixed-") or source != dict(entry="synthetic-scale", session=session,
                    branch="main", event=str(i)) or event["sequence"] != i or event["role"] != "user"
                    or event["text"] != f"合成并发写入样本 {i}。" or receipt["message_id"] != mid
                    or not receipt["original_saved"] or row[3] != "active"):
                raise ValueError("fixture source or receipt differs from its mixed workload")
            digest.update(json.dumps([mid, *row], ensure_ascii=True).encode() + b"\n")
            content = receipt["content_version"]
            lexical += bool(db.execute("""SELECT 1 FROM lexical_docs WHERE message_id=? AND content_version=?
                AND normalization_version=? AND EXISTS (SELECT 1 FROM lexical WHERE lexical MATCH ? AND message_id=?)""",
                (mid, content, NORMALIZATION_IDENTITY, f'message_id:"{mid}"', mid)).fetchone())
            vectors += bool(db.execute("""SELECT 1 FROM semantic_documents_v1 d JOIN semantic_chunks_v1 c
                USING(space_id,message_id,state_version,content_version) WHERE d.space_id=? AND d.message_id=?
                AND d.state_version=? AND d.content_version=? AND d.chunk_count=1 AND d.text_length=?
                AND c.start=0 AND c.end=d.text_length AND length(c.vector)=?""",
                (LocalMiniLM.space.key, mid, row[2], content, len(event["text"]), 4 * LocalMiniLM.space.dimensions)).fetchone())
    return {"originals_sha256": digest.hexdigest(), "lexical": lexical, "vectors": vectors}


def status(client, actor):
    reply = client.call(actor, "mcp", {"profile": "recall", "name": "evernook_v1_status", "arguments": {}}, timeout=180)
    return {"semantic": reply["semantic"], "lexical": reply["processing"]["lexical"],
            "single_writer": reply["service"]["single_writer"], "maintenance": reply["service"]["maintenance"]}


def run(args):
    report = json.loads((args.mixed / "result.json").read_bytes())
    samples = [json.loads(line) for line in (args.mixed / "samples.jsonl").read_bytes().splitlines()]
    ids = [item["write"]["message_id"] for item in samples]
    if (len(set(ids)) != 120 or [item["i"] for item in samples] != list(range(120))
            or report["acknowledged_and_replayed"] != 120 or report["after_restart"]["semantic"]["stored_vectors"] != 1_000_000):
        raise ValueError("requires the 120 acknowledged mixed-drill receipts")
    db = args.config.parent / "memory.sqlite3"
    initial = inspect_batch(db, ids)
    if initial["vectors"] != 0:
        raise ValueError("original pending batch has already been indexed")
    client = ServiceClient(args.config, reuse=True)
    actor = client.access_from_file(args.config.parent / "mcp-access-scale-writer.json")
    maintenance = client.access_from_file(args.maintenance_access)
    if actor.client_id != "synthetic-scale-writer" or maintenance.client_id != "scale-catchup-observer-maintenance":
        raise ValueError("dedicated fixture identities required")
    args.output.mkdir()
    with running_service(args, activate=False):
        if sorted(client.handshake(maintenance)["scopes"]) != ["accept", "maintain", "recall"]:
            raise ValueError("unexpected maintenance scopes")
        before = status(client, actor)
        if (not before["single_writer"] or before["maintenance"]["enabled"]
                or before["semantic"] != report["after_restart"]["semantic"]):
            raise ValueError("global coverage differs from the original fault-recovery receipt")
    client.close()
    receipt = {"installed_version": version("evernook"), "originals": len(ids), "initial": initial, "before": before}
    (args.output / "before.json").write_text(json.dumps(receipt, indent=2))
    with running_service(args, activate=False, maintenance_access=args.maintenance_access) as service:
        start = time.monotonic()
        with (args.output / "progress.jsonl").open("x") as out:
            while True:
                if service.poll() is not None:
                    raise RuntimeError("owned maintenance service exited")
                sample = inspect_batch(db, ids)
                if sample["originals_sha256"] != initial["originals_sha256"]:
                    raise ValueError("originals or receipts changed during maintenance")
                elapsed = time.monotonic() - start
                print(json.dumps({**sample, "seconds": round(elapsed, 3)}), file=out, flush=True)
                if sample["lexical"] == sample["vectors"] == 120:
                    break
                if elapsed >= args.max_seconds:
                    raise TimeoutError("pending receipts remained at the end of the observation window")
                time.sleep(5)
        client = ServiceClient(args.config, reuse=True)
        try:
            final = status(client, actor)
            sem, lex = final["semantic"], final["lexical"]
            if (not final["single_writer"] or not final["maintenance"]["enabled"]
                    or any(state["state"] != "ready" or state["source_messages"] != 1_000_120
                           or state["indexed_messages"] != 1_000_120 or state["pending_messages"] for state in (sem, lex))
                    or sem["stored_vectors"] != 1_000_120 or sem["failed_chunks"]):
                raise ValueError("public index coverage did not agree with the receipt-level readback")
            last = client.read(actor, ids[-1])
            page = client.call(actor, "recall", {"query": last.event.text, "limit": 10, "timeout": 120}, timeout=140)
            rank = next((i for i, hit in enumerate(page["hits"], 1) if hit["evidence"]["message_id"] == ids[-1]
                         and hit["evidence"]["source"] == asdict(last.event.source)), None)
            receipt.update({"elapsed_to_both_indexes_seconds": elapsed, "last_batch": sample, "after": final,
                            "last_original_recall_rank": rank, "recall_degraded": page["degraded"],
                            "backend": page["semantic_search_backend"], "scanned": page["semantic_scanned_chunks"]})
            (args.output / "result.json").write_text(json.dumps(receipt, indent=2))
            if rank is None:
                raise ValueError("newest original was missing from public recall")
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("config", "embedding-config", "context-config", "maintenance-access", "mixed", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, default=21600)
    run(parser.parse_args())
