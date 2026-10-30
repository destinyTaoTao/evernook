"""Real local candidate + CLI + stdio validation, using only synthetic external data."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters
from evernook import Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize, pair_client
from evernook.paths import external


async def verify(output: Path, embedding_config: Path) -> None:
    output = external(output)
    output.mkdir()  # Never overwrite a previous evidence run.
    config, owner = initialize(output / "data", client_id="synthetic-owner", entry="synthetic")
    access_file = config.parent / "mcp-access-owner.json"
    pair_client(config, client_id=owner.client_id, entry="synthetic", access_path=access_file)
    runtime = MemoryRuntime(config)
    backend = LocalMiniLM.from_file(embedding_config)
    workflow = SemanticRecall(runtime, backend)
    corpus = [
        ("food", "2021-01-01T00:00:00Z", "合成：我喜欢吃辣，经常选麻辣火锅。"),
        ("commute", "2022-01-01T00:00:00Z", "合成：我每天骑自行车去公司。"),
        ("noise", "2026-10-29T00:00:00Z", "合成：这周天气多云，明天可能有雨。"),
        ("protected", "2026-10-29T00:00:00Z", "password=synthetic-candidate-sentinel-123"),
    ]
    ids = {}
    for sequence, (name, occurred, text) in enumerate(corpus):
        receipt = runtime.submit(owner, Event(Source("synthetic", "s1", "main", name), "user", sequence, occurred, "complete", text))
        ids[name] = receipt.message_id
    started = time.perf_counter()
    indexed = workflow.refresh(owner)
    indexing_seconds = time.perf_counter() - started
    assert indexed.state == "ready" and indexed.indexed_messages == 3
    runtime.refresh_lexical(owner)
    queries = [("饮食偏好", "food"), ("上下班交通方式", "commute"), ("麻辣火锅", "food"), ("月球量子胡萝卜充电", None)]
    results = []
    for query, expected in queries:
        started = time.perf_counter()
        page = workflow.recall(owner, query, as_of="2026-10-30T00:00:00Z")
        actual = [hit.evidence.message_id for hit in page.hits]
        results.append({"query": query, "expected": ids.get(expected) if expected else None,
                        "seconds": time.perf_counter() - started, "result": asdict(page)})
        if expected:
            assert ids[expected] in actual
        for hit in page.hits:
            original = runtime.read(owner, hit.evidence.message_id)
            assert original is not None
            for span in hit.evidence.spans:
                assert original.event.text[span.start:span.end] == span.text
    command = [sys.executable, "-m", "evernook.cli", "recall", "--config", str(config),
               "--access", str(access_file), "--embedding-config", str(embedding_config)]
    cli = subprocess.run(command, input=json.dumps({"query": "饮食偏好"}).encode(), capture_output=True, timeout=60, check=True)
    cli_result = json.loads(cli.stdout)
    assert cli_result["ok"] and cli_result["result"]["hits"][0]["evidence"]["message_id"] == ids["food"]
    assert owner.token.encode() not in cli.stdout + cli.stderr
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "evernook.mcp_server", "--config", str(config),
        "--access", str(access_file), "--embedding-config", str(embedding_config)])
    async with Client(parameters, read_timeout_seconds=60) as client:
        recalled = await client.call_tool("evernook_v1_recall", {"query": "上下班交通方式"})
        assert not recalled.is_error and recalled.structured_content is not None
        assert recalled.structured_content["result"]["hits"][0]["evidence"]["message_id"] == ids["commute"]
        mcp_result = recalled.structured_content
    timed_out = workflow.recall(owner, "麻辣火锅", timeout=0.001)
    assert "embedding_timeout" in timed_out.degraded and timed_out.hits
    missing_backend = LocalMiniLM(node=backend.node, transformers_dir=backend.transformers_dir, model_dir=output / "missing-model")
    missing = SemanticRecall(runtime, missing_backend).recall(owner, "麻辣火锅")
    assert "embedding_unavailable" in missing.degraded and missing.hits
    report = {"space": asdict(backend.space), "corpus": corpus, "ids": ids, "index": asdict(indexed),
              "indexing_seconds": indexing_seconds, "queries": results, "cli": cli_result, "mcp": mcp_result,
              "timeout": asdict(timed_out), "missing_model": asdict(missing),
              "network_boundary": "local_files_only; successful worker response requires zero Node fetch/socket attempts; OS networking was not disabled"}
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "semantic_queries": 2, "literal_query": 1,
                      "no_answer_candidates": len(results[-1]["result"]["hits"]),
                      "cli": True, "mcp": True, "timeout_fallback": True, "missing_model_fallback": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.output, args.embedding_config))
