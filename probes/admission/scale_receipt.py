"""Reconcile a stopped scale writer through public reads; never fill missing scale cells."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from evernook import Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source
from evernook.paths import external
from scale_ingest import save


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ingest_path = args.fixture / 'ingest-report.json'
    ingest = json.loads(ingest_path.read_text(encoding='utf8')) if ingest_path.exists() else None
    manifest = json.loads((args.fixture / "manifest.json").read_text(encoding="utf-8"))
    policy_raw = Path(__file__).with_name("policy.json").read_bytes()
    policy = json.loads(policy_raw)
    if (manifest["fixture_version"] != policy["scale"]["fixture_version"]
            or manifest["policy_sha256"] != hashlib.sha256(policy_raw).hexdigest()):
        raise ValueError("fixture_identity_mismatch")
    root = external(args.output)
    root.mkdir()
    runtime = MemoryRuntime(args.fixture / "runtime/runtime.json")
    actor = runtime.access_from_file(args.fixture / "runtime/mcp-access-scale-writer.json")
    actual = runtime.status(actor).messages
    semantic = SemanticRecall(runtime, LocalMiniLM.from_file(args.embedding_config)).status(actor)
    topics = ("湖边步道观察记录", "厨房食材库存记录", "天文摄影曝光记录", "编辑器快捷键练习",
              "图书馆书架整理记录", "公交时刻抄录练习", "花园植物浇水记录", "散步路线观察笔记")
    selected = []
    for index in sorted({0, 1, actual // 2, actual - 1}):
        if not 0 <= index < actual:
            continue
        source = Source("synthetic-scale", "zh-scale-v1", "main", f"m{index:07d}")
        message = runtime.read_source(actor, source)
        expected = policy["scale"]["pure_semantic_original"] if index == 0 else (
            f"合成条目{index:07d}：{topics[index % len(topics)]}；保留数字37.50与🙂。")
        when = (datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(days=index % 1461)
                if index < manifest['expected_messages'] * 3 // 4 else datetime(2026, 11, 3, tzinfo=timezone.utc))
        expected_event = Event(source, 'user', index, when.isoformat(), 'complete', expected)
        selected.append({"index": index, "message_id": message.receipt.message_id if message else None,
            "exact_original": message is not None and message.receipt.original_saved and message.event == expected_event})
    complete = ingest is not None and actual == ingest['completed'] == ingest['expected_messages'] == manifest['expected_messages']
    save(root / "report.json", {"expected_messages": manifest['expected_messages'], "actual_messages": actual,
        "writer_report_count_matches": actual == ingest['actual_messages'] if ingest is not None else None,
        'writer_report_missing': ingest is None, 'complete_fixture': complete,
        "ingest_error_code": ingest['error_code'] if ingest is not None else 'writer_report_missing',
        "semantic": asdict(semantic), "selected_roundtrips": selected,
        "ready_for_full_scale_query_matrix": complete, "admission": "blocked" if not complete else "pending",
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Post-writer public status/selected-source reconciliation only. Missing target-size matrix, cold/warm timings and coverage remain unmeasured; no extrapolation or inferred vector counts."})
    return 0 if complete and all(row["exact_original"] for row in selected) else 2


if __name__ == "__main__":
    raise SystemExit(main())
