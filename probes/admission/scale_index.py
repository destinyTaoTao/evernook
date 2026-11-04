"""Build real derived indexes through maintenance APIs under the frozen time budget."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall, pair_client
from evernook.paths import external
from scale_ingest import save


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = external(args.output)
    root.mkdir()
    policy_raw = Path(__file__).with_name("policy.json").read_bytes()
    policy = json.loads(policy_raw)
    config = external(args.fixture) / "runtime" / "runtime.json"
    runtime = MemoryRuntime(config)
    access_file = config.parent / "mcp-access-scale-maintenance.json"
    access = (runtime.access_from_file(access_file) if access_file.exists() else pair_client(config,
        client_id="synthetic-scale-maintenance", entry="synthetic-maintenance", access_path=access_file,
        scopes=("maintain", "recall")))
    semantic = SemanticRecall(runtime, LocalMiniLM.from_file(args.embedding_config))
    save(root / "manifest.json", {"fixture": str(args.fixture), "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
         "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         "order": "semantic first to measure >10000 real fragments, then lexical; both share one frozen index time budget"})
    started = time.perf_counter()
    calls = 0
    state = semantic.status(access)
    lexical = runtime.lexical_status(access)
    error = None
    try:
        with (root / "progress.jsonl").open("w", encoding="utf-8") as log:
            while state.pending_messages and (state.stored_vectors or 0) < policy["scale"]["index_vectors_target"]:
                if time.perf_counter() - started >= policy["scale"]["index_seconds_max"]:
                    error = "index_time_budget"
                    break
                before = time.perf_counter()
                state = semantic.refresh(access, limit=16, timeout=120)
                calls += 1
                row = {"phase": "semantic", "calls": calls, "elapsed_seconds": time.perf_counter() - started,
                       "refresh_ms": (time.perf_counter() - before) * 1000, "semantic": asdict(state)}
                log.write(json.dumps(row) + "\n")
                log.flush()
                if calls % 10 == 0:
                    print(json.dumps(row), flush=True)
            while lexical.pending_messages and error is None:
                if time.perf_counter() - started >= policy["scale"]["index_seconds_max"]:
                    error = "index_time_budget"
                    break
                before = time.perf_counter()
                lexical = runtime.refresh_lexical(access, limit=100)
                row = {"phase": "lexical", "elapsed_seconds": time.perf_counter() - started,
                       "refresh_ms": (time.perf_counter() - before) * 1000, "lexical": asdict(lexical)}
                log.write(json.dumps(row) + "\n")
                log.flush()
    except Exception as failure:
        error = type(failure).__name__
    save(root / "report.json", {"elapsed_seconds": time.perf_counter() - started, "semantic_refresh_calls": calls,
         "semantic": asdict(semantic.status(access)), "lexical": asdict(runtime.lexical_status(access)),
         "actual_messages": runtime.status(access).messages, "error_code": error,
         "admission": "blocked" if error or state.state != "ready" or lexical.state != "ready" else "pending"})
    return 2 if error or state.state != "ready" or lexical.state != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
