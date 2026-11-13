"""Synthetic, frozen-suite E5 experiment; this is not a production provider."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from typing import Any
from unittest.mock import patch

import evernook
from evernook import LocalMiniLM
from evernook.embedding import EmbeddingSpace
from evernook.learning_evaluation import load_suite
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.safety import credential_signal
from evernook.semantic import RecallPage, SemanticRecall, SemanticStatus
from probes.admission import learning

REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
SUITE = 'b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1'
FILES = {
    'config.json': 'cb99455288675345e1a4f411438d5d0adbba5fbd3a67ea4fb03c015433b996c1',
    'tokenizer.json': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39',
    'tokenizer_config.json': 'a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b',
    'special_tokens_map.json': 'd05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7',
    'sentencepiece.bpe.model': 'cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865',
    'onnx/model_quantized.onnx': 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193',
}


class Candidate:
    space = EmbeddingSpace('Xenova/multilingual-e5-small', REVISION, 384,
        hashlib.sha256(json.dumps(FILES, sort_keys=True).encode()).hexdigest(), encoding='probe-e5-prefix-mean-l2-v1')

    def __init__(self, model: Path, transformers: Path, node: Path):
        self.model, self.transformers, self.node = external(model), external(transformers), node.resolve()
        if not node.is_absolute():
            raise ValueError('node_must_be_absolute')
        self.role: str | None = None

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('embedding_canceled')
        if self.role not in {'query', 'passage'}:
            raise MemoryFailure('embedding_role_missing')
        for name, expected in FILES.items():
            path = (self.model / name).resolve()
            if not path.is_relative_to(self.model):
                raise MemoryFailure('embedding_unavailable')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise MemoryFailure('embedding_integrity_mismatch')
        prefixed = tuple(self.role + ': ' + text for text in texts)
        if any(not text or len(text) > 256 or credential_signal(text) for text in prefixed):
            raise MemoryFailure('protected_embedding_input')
        payload = json.dumps({'model_dir': str(self.model), 'transformers_dir': str(self.transformers),
                              'texts': prefixed}, ensure_ascii=False).encode()
        try:
            if evernook.__file__ is None:
                raise MemoryFailure('embedding_unavailable')
            worker = Path(evernook.__file__).with_name('embedding_worker.mjs')
            completed = subprocess.run([str(self.node), str(worker)], input=payload, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=timeout, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            value = json.loads(completed.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            raise MemoryFailure('embedding_unavailable') from None
        if value.get('network_attempts') != 0 or len(value.get('vectors', [])) != len(texts):
            raise MemoryFailure('embedding_unavailable')
        return tuple(None if vector is None else tuple(vector) for vector in value['vectors'])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--transformers-dir', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    suite = Path('eval/zh-learning-v1')
    if load_suite(suite).suite_hash != SUITE:
        raise RuntimeError('unexpected_frozen_suite')
    backend = Candidate(args.model_dir, args.transformers_dir, args.node)

    class ProbeRecall(SemanticRecall):
        def refresh(self, *a: Any, **kw: Any) -> SemanticStatus:
            backend.role = 'passage'
            try:
                return super().refresh(*a, **kw)
            finally:
                backend.role = None

        def recall(self, *a: Any, **kw: Any) -> RecallPage:
            backend.role = 'query'
            try:
                return super().recall(*a, **kw)
            finally:
                backend.role = None

    # This serial, synthetic-only experiment supplies roles without changing production interfaces.
    with patch.object(LocalMiniLM, 'from_file', return_value=backend), patch.object(learning, 'SemanticRecall', ProbeRecall):
        learning.collect(suite, args.output, args.model_dir / 'not-used', lexical_ready=True)


if __name__ == '__main__':
    main()
