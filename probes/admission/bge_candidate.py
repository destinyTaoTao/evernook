"""Synthetic, frozen-suite BGE experiment; this is not a production provider."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

from evernook import LocalMiniLM
from evernook.embedding import EmbeddingSpace
from evernook.learning_evaluation import load_suite
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.safety import credential_signal
from probes.admission import learning

REVISION = '75c43b069aac4d136ba6bc1122f995fedcfd2781'
SUITE = 'b96415eb4800e1afd51834ae0740f518adacacd3bc166ef679a43911ea5d8ae1'
FILES = {
    'config.json': 'd4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f',
    'tokenizer.json': '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26',
    'tokenizer_config.json': 'e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a',
    'special_tokens_map.json': 'b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3',
    'vocab.txt': '45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c',
    'onnx/model_quantized.onnx': '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc',
}


class Candidate:
    space = EmbeddingSpace('Xenova/bge-small-zh-v1.5', REVISION, 512,
        hashlib.sha256(json.dumps(FILES, sort_keys=True).encode()).hexdigest(), encoding='asymmetric-bge-zh-cls-l2-v1')

    def __init__(self, model: Path, transformers: Path, node: Path):
        self.model, self.transformers, self.node = external(model), external(transformers), node.resolve()
        if not node.is_absolute():
            raise ValueError('node_must_be_absolute')

    def embed(self, texts: tuple[str, ...], *, timeout: float,
              cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        raise MemoryFailure('embedding_role_missing')

    def embed_documents(self, texts: tuple[str, ...], *, timeout: float,
                        cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        return self._embed(texts, timeout=timeout, cancel=cancel)

    def embed_query(self, texts: tuple[str, ...], *, timeout: float,
                    cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        return self._embed(tuple('为这个句子生成表示以用于检索相关文章：' + text for text in texts), timeout=timeout, cancel=cancel)

    def _embed(self, texts: tuple[str, ...], *, timeout: float,
               cancel: threading.Event | None = None) -> tuple[tuple[float, ...] | None, ...]:
        if cancel is not None and cancel.is_set():
            raise MemoryFailure('embedding_canceled')
        for name, expected in FILES.items():
            path = (self.model / name).resolve()
            if not path.is_relative_to(self.model):
                raise MemoryFailure('embedding_unavailable')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise MemoryFailure('embedding_integrity_mismatch')
        if any(not text or len(text) > 256 or credential_signal(text) for text in texts):
            raise MemoryFailure('protected_embedding_input')
        payload = json.dumps({'model_dir': str(self.model), 'transformers_dir': str(self.transformers),
                              'texts': texts}, ensure_ascii=False).encode()
        try:
            worker = Path(__file__).with_name('bge_worker.mjs')
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

    # Synthetic-only injection; production provider selection remains unchanged.
    with patch.object(LocalMiniLM, 'from_file', return_value=backend):
        learning.collect(suite, args.output, args.model_dir / 'not-used', lexical_ready=True)


if __name__ == '__main__':
    main()
