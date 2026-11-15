"""Real pinned embedding equivalence and separate one-shot/session timing."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import time

from evernook.embedding import LocalMiniLM
from evernook.paths import external
from probes.admission.embedding_file_access import TEXTS


def collect(config: Path, output: Path, baseline: Path, samples: int) -> None:
    if not 1 <= samples <= 100:
        raise ValueError('invalid_sample_count')
    output = external(output)
    output.mkdir()
    previous = json.loads(baseline.read_text(encoding='utf8'))
    backend = LocalMiniLM.from_file(config)
    if previous['space'] != backend.space.key or previous['texts'] != list(TEXTS):
        raise ValueError('baseline_identity_mismatch')
    reference = previous['records'][0]['vectors']
    result: dict[str, object] = {'space': asdict(backend.space), 'texts': TEXTS, 'vectors': reference,
        'samples_per_mode': samples, 'admission': 'pending', 'modes': {},
        'scope': 'Public embedding only; OS cache uncontrolled; other owned scale ingestion may overlap. Not whole-memory/scale admission.'}
    modes: dict[str, object] = {}
    old_options, old_trace = os.environ.get('NODE_OPTIONS'), os.environ.get('EVERNOOK_EMBEDDING_READ_TRACE')
    try:
        for mode in ('one_shot', 'session'):
            trace = output / (mode + '-worker-trace.jsonl')
            os.environ['NODE_OPTIONS'] = '--import=' + Path(__file__).with_name('embedding_reads.mjs').resolve().as_uri()
            os.environ['EVERNOOK_EMBEDDING_READ_TRACE'] = str(trace)
            records = []
            context = backend.session(timeout=120) if mode == 'session' else nullcontext(backend)
            with context as active:
                for index in range(samples + 1):
                    started = time.perf_counter()
                    vectors = active.embed(TEXTS, timeout=20)
                    elapsed = (time.perf_counter() - started) * 1000
                    encoded = json.dumps(vectors, separators=(',', ':')).encode()
                    equal = json.loads(encoded) == reference
                    records.append({'index': index, 'elapsed_ms': elapsed, 'vectors_equal': equal,
                                    'vectors_sha256': hashlib.sha256(encoded).hexdigest()})
                    if not equal:
                        raise ValueError('embedding_session_vector_changed')
            trace_rows = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            starts = sum(row['event'] == 'worker_started' for row in trace_rows)
            if starts != (1 if mode == 'session' else samples + 1):
                raise ValueError('unexpected_worker_starts')
            ordered = sorted(row['elapsed_ms'] for row in records[1:])
            modes[mode] = {'worker_starts': starts, 'first_call_ms': records[0]['elapsed_ms'],
                'observed_calls_after_first': samples, 'p50_ms': ordered[(samples * 50 + 99) // 100 - 1],
                'p95_ms': ordered[(samples * 95 + 99) // 100 - 1], 'records': records}
            result['modes'] = modes
            (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
            print(json.dumps({'mode': mode, 'worker_starts': starts, 'p50_ms': ordered[(samples * 50 + 99) // 100 - 1],
                              'p95_ms': ordered[(samples * 95 + 99) // 100 - 1]}), flush=True)
    finally:
        for key, old in (('NODE_OPTIONS', old_options), ('EVERNOOK_EMBEDDING_READ_TRACE', old_trace)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--embedding-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    collect(args.embedding_config, args.output, args.baseline, args.samples)
