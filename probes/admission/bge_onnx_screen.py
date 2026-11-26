"""Offline Python ORT vs the fixed Transformers.js BGE worker on synthetic Chinese texts."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import threading
import time
from typing import Any

from evernook.learning_evaluation import load_suite
from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.safety import credential_signal
from probes.admission.bge_candidate import Candidate, FILES, SUITE


VectorBatch = tuple[tuple[float, ...] | None, ...]


def load_native(model: Path) -> tuple[Any, Any]:
    ort = importlib.import_module("onnxruntime")
    tokenizer_class = importlib.import_module("tokenizers").Tokenizer
    model = external(model)
    for name, digest in FILES.items():
        path = (model / name).resolve()
        if not path.is_relative_to(model):
            raise ValueError("unexpected BGE asset path")
        with path.open("rb") as asset:
            if hashlib.file_digest(asset, "sha256").hexdigest() != digest:
                raise ValueError("unexpected BGE asset")
    tokenizer = tokenizer_class.from_file(str(model / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.enable_padding(pad_id=tokenizer.token_to_id("[PAD]"), pad_token="[PAD]")
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    session = ort.InferenceSession(str(model / "onnx/model_quantized.onnx"), options, providers=["CPUExecutionProvider"])
    return tokenizer, session


class NativeBGE:
    """Isolated evaluation backend. No production configuration can select it."""
    def __init__(self, model: Path) -> None:
        self.np = importlib.import_module("numpy")
        self.tokenizer, self.session = load_native(model)
        self.space = replace(Candidate.space, encoding=f"asymmetric-bge-zh-cls-ort-{version('onnxruntime')}-tok-{version('tokenizers')}")

    def embed(self, texts: tuple[str, ...], *, timeout: float,
                  cancel: threading.Event | None = None) -> VectorBatch:
        raise MemoryFailure("embedding_role_missing")

    def embed_documents(self, texts: tuple[str, ...], *, timeout: float,
                  cancel: threading.Event | None = None) -> VectorBatch:
        return self._embed(texts, timeout, cancel)

    def embed_query(self, texts: tuple[str, ...], *, timeout: float,
                  cancel: threading.Event | None = None) -> VectorBatch:
        return self._embed(tuple("为这个句子生成表示以用于检索相关文章：" + text for text in texts), timeout, cancel)

    def _embed(self, texts: tuple[str, ...], timeout: float, cancel: threading.Event | None) -> VectorBatch:
        deadline = time.monotonic() + timeout

        def check() -> None:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure("embedding_canceled")
            if time.monotonic() >= deadline:
                raise MemoryFailure("embedding_timeout")

        check()
        if any(not text or len(text) > 256 or credential_signal(text) for text in texts):
            raise MemoryFailure("protected_embedding_input")
        batch = self.tokenizer.encode_batch(list(texts))
        valid = [s for s, item in zip(texts, batch) if sum(item.attention_mask) <= 512]
        if not valid:
            return (None,) * len(texts)
        selected = batch if len(valid) == len(texts) else self.tokenizer.encode_batch(valid)
        feeds = {name: self.np.asarray([getattr(item, field) for item in selected], dtype=self.np.int64)
                 for name, field in (("input_ids", "ids"), ("attention_mask", "attention_mask"), ("token_type_ids", "type_ids"))}
        check()
        raw = self.session.run(["last_hidden_state"], feeds)[0][:, 0, :]
        norms = self.np.linalg.norm(raw, axis=1, keepdims=True)
        if raw.shape != (len(valid), self.space.dimensions) or not self.np.isfinite(norms).all() or (norms == 0).any():
            raise MemoryFailure("invalid_embedding")
        vectors = iter((raw / norms).tolist())
        check()
        return tuple(tuple(next(vectors)) if sum(item.attention_mask) <= 512 else None for item in batch)


def compare(model: Path, transformers: Path, node: Path) -> dict[str, object]:
    np = importlib.import_module("numpy")
    corpus = Path(__file__).resolve().parents[2] / "eval/zh-learning-v1/corpus.json"
    documents = tuple(source["text"] for source in json.loads(corpus.read_text(encoding="utf-8"))["sources"][:8])
    queries = ("我以前在哪里见过红嘴蓝鹊？", "远游时我会先找什么？", "谁的兴趣是摄影？", "为什么要留电量？")
    started = time.perf_counter()
    backend = NativeBGE(model)
    startup = time.perf_counter() - started
    reference = Candidate(model, transformers, node)
    native_vectors, reference_vectors = {}, {}
    results: dict[str, object] = {"python_session_startup_seconds": startup}
    for role, texts in (("documents", documents), ("queries", queries)):
        started = time.perf_counter()
        embed_native = backend.embed_query if role == "queries" else backend.embed_documents
        native = np.asarray(embed_native(texts, timeout=30), dtype=np.float64)
        native_time = time.perf_counter() - started
        started = time.perf_counter()
        embed = reference.embed_query if role == "queries" else reference.embed_documents
        old = np.asarray(embed(texts, timeout=30), dtype=np.float64)
        if native.shape != old.shape or not np.isfinite(native).all() or not np.isfinite(old).all():
            raise ValueError("invalid model output")
        native_vectors[role], reference_vectors[role] = native, old
        results[role] = {"count": len(texts), "native_batch_seconds": native_time,
                         "node_one_shot_seconds": time.perf_counter() - started,
                         "max_absolute_difference": float(np.abs(native - old).max()),
                         "min_paired_cosine": float(np.sum(native * old, axis=1).min())}
    ranks = [np.argsort(-(v["queries"] @ v["documents"].T), axis=1, kind="stable")
             for v in (native_vectors, reference_vectors)]
    results["identical_document_rankings"] = int(np.all(ranks[0] == ranks[1], axis=1).sum())
    results["versions"] = {name: version(name) for name in ("numpy", "onnxruntime", "tokenizers")}
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("model-dir", "transformers-dir", "node"):
        parser.add_argument("--" + arg, type=Path, required=True)
    parser.add_argument("--frozen-output", type=Path)
    parser.add_argument("--frozen-backend", choices=("native", "node"), default="native")
    parser.add_argument("--previous-core-python", type=Path)
    args = parser.parse_args()
    if args.frozen_output is None:
        print(json.dumps(compare(args.model_dir, args.transformers_dir, args.node)))
    else:
        from probes.admission.learning import _collect
        suite = Path(__file__).resolve().parents[2] / "eval/zh-learning-v1"
        if load_suite(suite).suite_hash != SUITE:
            raise ValueError("unexpected frozen suite")
        selected = (NativeBGE(args.model_dir) if args.frozen_backend == "native"
                    else Candidate(args.model_dir, args.transformers_dir, args.node))
        _collect(suite, args.frozen_output, selected, lexical_ready=True,
                 full_lifecycle=True, max_fixture_sources=10004, previous_core_python=args.previous_core_python)
