"""Independent synthetic control-history retrieval probe; not an admission scorer."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time

from evernook import Change, Event, MemoryRuntime, SemanticRecall, Source, initialize
from evernook.paths import external
from probes.admission.bge_candidate import Candidate


CASES = (
    {'id': 'suppression', 'action': 'suppress',
     'original': '合成测试：纸鹤放在蓝色抽屉。', 'confirmation': '合成测试：确认停止普通召回纸鹤的记录。',
     'questions': (('fact', '纸鹤放在哪里？'), ('fact', '纸鹤现在放在什么地方？'),
                   ('history', '之前是否停止召回纸鹤的记录？'), ('history', '我对纸鹤的记录做过什么处理？'))},
    {'id': 'revocation', 'action': 'revoke',
     'original': '合成测试：旅行地图放在木盒里。', 'confirmation': '合成测试：确认撤销旅行地图的记录。',
     'questions': (('fact', '旅行地图放在哪里？'), ('fact', '我的旅行地图现在在哪里？'),
                   ('history', '旅行地图的记录是否被撤销过？'), ('history', '我对旅行地图的记录做过什么处理？'))},
)


def collect(output: Path, backend: Candidate) -> None:
    root = external(output)
    root.mkdir()
    results: list[dict[str, object]] = []
    for case in CASES:
        config, access = initialize(root / str(case['id']), client_id='synthetic-control', entry='fixture')
        runtime = MemoryRuntime(config)
        original = runtime.submit(access, Event(Source('fixture', str(case['id']), 'main', 'original'),
            'user', 0, '2024-01-01T00:00:00Z', 'complete', str(case['original'])))
        confirmation = runtime.submit(access, Event(Source('fixture', str(case['id']), 'main', 'confirmation'),
            'user', 1, '2024-01-01T00:01:00Z', 'complete', str(case['confirmation'])))
        runtime.change(access, Change('synthetic-control', original.message_id, 1, str(case['action']),
            confirmation.message_id, 'synthetic fixture'))
        while runtime.lexical_status(access).pending_messages:
            runtime.refresh_lexical(access)
        semantic = SemanticRecall(runtime, backend)
        while semantic.status(access).pending_messages:
            semantic.refresh(access, limit=16, timeout=60)
        for expected, question in case['questions']:
            for arm, recall in (('lexical', SemanticRecall(runtime, None)), ('bge', semantic)):
                start = time.perf_counter()
                page = recall.recall(access, question, limit=10, as_of='2026-11-13T01:00:00Z', timeout=30)
                if any(hit.evidence.message_id == original.message_id for hit in page.hits):
                    raise RuntimeError('controlled_original_returned')
                results.append({'case': case['id'], 'question_kind': expected, 'question': question, 'arm': arm,
                    'elapsed_ms': round((time.perf_counter() - start) * 1000, 2),
                    'hits': [{'source': hit.evidence.source.event, 'channels': hit.channels,
                              'similarity': hit.similarity, 'score': hit.score} for hit in page.hits],
                    'state': page.evidence_state, 'degraded': page.degraded})
    (root / 'results.json').write_text(json.dumps({'model': asdict(backend.space), 'rows': results},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--transformers-dir', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.output, Candidate(args.model_dir, args.transformers_dir, args.node))


if __name__ == '__main__':
    main()
