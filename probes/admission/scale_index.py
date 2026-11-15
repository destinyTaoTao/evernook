"""Build real derived indexes through maintenance APIs under the frozen time budget."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import time

from evernook import LocalMiniLM, MemoryRuntime, SemanticRecall, pair_client
from evernook.embedding import ServiceEmbedding
from evernook.models import MemoryFailure
from evernook.paths import external
from probes.admission.scale_ingest import save


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--embedding-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--reuse-embedding', action='store_true')
    parser.add_argument('--max-seconds', type=float, help='Diagnostic guard; never changes the frozen admission budget')
    parser.add_argument('--semantic-window', type=int, default=16, help='Explicit bounded maintenance window (16..1000)')
    parser.add_argument('--lexical-strategy', choices=('default', 'checkpoint'), default='default')
    parser.add_argument('--full-semantic', action='store_true', help='Require every eligible source, beyond the initial vector smoke target')
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
    binding = ServiceEmbedding(args.embedding_config) if args.reuse_embedding else None
    semantic = SemanticRecall(runtime, binding.current() if binding else LocalMiniLM.from_file(args.embedding_config))
    maximum = min(policy['scale']['index_seconds_max'], args.max_seconds) if args.max_seconds else policy['scale']['index_seconds_max']
    if not math.isfinite(maximum) or not 0 < maximum <= policy['scale']['index_seconds_max'] or not 16 <= args.semantic_window <= 1000:
        raise ValueError('invalid_index_budget')
    save(root / "manifest.json", {"fixture": str(args.fixture), "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
         "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'reuse_embedding': args.reuse_embedding, 'execution_budget_seconds': maximum,
         'semantic_window': args.semantic_window,
         'lexical_strategy': args.lexical_strategy, 'full_semantic_attempt': args.full_semantic,
         "order": "semantic first to measure >10000 real fragments, then lexical; both share one frozen index time budget"})
    started = time.perf_counter()
    calls = 0
    state = semantic.status(access)
    lexical = runtime.lexical_status(access)
    error = None
    try:
        if args.lexical_strategy == 'checkpoint':
            runtime.configure_lexical_acceleration(access, enabled=True)
            runtime.configure_lexical_id_acceleration(access, enabled=True)
            runtime.configure_lexical_checkpoint(access, enabled=True)
        with (root / "progress.jsonl").open("w", encoding="utf-8") as log:
            while state.pending_messages and (args.full_semantic or (state.stored_vectors or 0) < policy["scale"]["index_vectors_target"]):
                if time.perf_counter() - started >= maximum:
                    error = "index_time_budget"
                    break
                before = time.perf_counter()
                remaining = maximum - (time.perf_counter() - started)
                if args.semantic_window == 16:
                    state = semantic.refresh(access, limit=16, timeout=min(30, remaining))
                else:
                    state = semantic.refresh_window(access, limit=args.semantic_window, timeout=min(90, remaining))
                calls += 1
                row = {"phase": "semantic", "calls": calls, "elapsed_seconds": time.perf_counter() - started,
                       "refresh_ms": (time.perf_counter() - before) * 1000, "semantic": asdict(state)}
                log.write(json.dumps(row) + "\n")
                log.flush()
                if calls % 10 == 0:
                    print(json.dumps(row), flush=True)
            while lexical.pending_messages and error is None:
                if time.perf_counter() - started >= maximum:
                    error = "index_time_budget"
                    break
                before = time.perf_counter()
                lexical = runtime.refresh_lexical_window(access, limit=5000,
                    budget_seconds=min(120, maximum - (time.perf_counter() - started)))
                row = {"phase": "lexical", "elapsed_seconds": time.perf_counter() - started,
                       "refresh_ms": (time.perf_counter() - before) * 1000, "lexical": asdict(lexical)}
                log.write(json.dumps(row) + "\n")
                log.flush()
    except Exception as failure:
        error = failure.code if isinstance(failure, MemoryFailure) else type(failure).__name__
    finally:
        if binding:
            binding.close()
    save(root / "report.json", {"elapsed_seconds": time.perf_counter() - started, "semantic_refresh_calls": calls,
         "semantic": asdict(semantic.status(access)), "lexical": asdict(runtime.lexical_status(access)),
         "actual_messages": runtime.status(access).messages, "error_code": error,
         "admission": "blocked" if error or state.state != "ready" or lexical.state != "ready" else "pending"})
    return 2 if error or state.state != "ready" or lexical.state != "ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
