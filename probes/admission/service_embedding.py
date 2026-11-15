"""Measure actual installed local service context calls on a small synthetic fixture."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from evernook import Event, LocalMiniLM, MemoryRuntime, SemanticRecall, Source, initialize, pair_client
from evernook.host_context import HostContext
from evernook.paths import external
from evernook.service import ServiceClient
from probes.admission.learning import implementation_identity, save


def collect(output: Path, embedding: Path, context: Path) -> None:
    root = external(output); root.mkdir()
    config, access = initialize(root / 'runtime', client_id='synthetic-service', entry='fixture')
    runtime = MemoryRuntime(config)
    for index, text in enumerate(('合成：目前使用Java21。', '合成：周末喜欢沿湖散步。', '合成：编辑器里使用快捷键练习。')):
        runtime.submit(access, Event(Source('fixture', 'synthetic', 'main', str(index)), 'user', index,
            '2026-11-03T00:00:00+00:00', 'complete', text))
    recall = SemanticRecall(runtime, LocalMiniLM.from_file(embedding))
    recall.refresh(access)
    semantic_before = asdict(recall.status(access))
    runtime.refresh_lexical(access)
    model = HostContext(runtime, recall, context).status()['model_id']
    owner = config.parent / 'mcp-access-service-owner.json'
    pair_client(config, client_id=access.client_id, entry='fixture', access_path=owner)
    records: list[dict[str, Any]] = []
    with (root / 'service-stderr.log').open('wb') as stderr:
        child = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
            '--activate-owner', str(owner), '--embedding-config', str(embedding), '--context-config', str(context)],
            stdout=subprocess.DEVNULL, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 20
            while not (config.parent / 'memory-service.json').exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('service_not_ready')
                time.sleep(.05)
            client = ServiceClient(config)
            for index in range(31):
                started = time.perf_counter()
                result = client.context(access, '周末怎样放松？', model_id=model, max_tokens=4096,
                    as_of='2026-11-04T00:00:00Z', timeout=30)
                records.append({'index': index, 'elapsed_ms': (time.perf_counter() - started) * 1000,
                    'context': result})
                save(root / 'records.json', records)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
    ordered = sorted(row['elapsed_ms'] for row in records[1:])
    save(root / 'report.json', {'implementation': implementation_identity(), 'calls': len(records),
        'first_ms': records[0]['elapsed_ms'], 'post_first_samples': 30, 'p50_ms': ordered[14], 'p95_ms': ordered[28],
        'semantic_before_service': semantic_before,
        'scope': 'Installed service IPC + context on 3 originals, 30s call budgets; OS cache uncontrolled, concurrent scale jobs. Not scale/host/model admission.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'embedding', 'context'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    collect(args.output, args.embedding, args.context)
