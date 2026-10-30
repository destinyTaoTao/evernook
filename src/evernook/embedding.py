"""Explicit local MiniLM candidate; no downloader, daemon or cloud fallback."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Protocol

from .models import MemoryFailure
from .paths import external
from .runtime import _json, _load
from .safety import credential_signal


@dataclass(frozen=True)
class EmbeddingSpace:
    model: str
    revision: str
    dimensions: int
    artifact_digest: str
    chunk_size: int = 192
    overlap: int = 32
    encoding: str = "mean-l2-f32-v1"

    @property
    def key(self) -> str:
        return hashlib.sha256(_json(asdict(self)).encode()).hexdigest()


class EmbeddingBackend(Protocol):
    @property
    def space(self) -> EmbeddingSpace: ...

    def embed(self, texts: tuple[str, ...], *, timeout: float) -> tuple[tuple[float, ...], ...]: ...


def unit_vector(values: object, dimensions: int) -> tuple[float, ...]:
    if not isinstance(values, (list, tuple)) or len(values) != dimensions:
        raise MemoryFailure("invalid_embedding")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise MemoryFailure("invalid_embedding")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise MemoryFailure("invalid_embedding")
    return tuple(value / norm for value in values)


# Hashes from the already prepared, pinned R0 candidate. No weights are distributed.
_FILES = {
    "config.json": "05b570bff786faa5c4604152aa16f19f77ed6dfc31e47dd0f3dd987078693ac7",
    "onnx/model_quantized.onnx": "66fc00f5f29afcaff34092e1bdd20008ca3918265a82fb9695a551e510cc4ebc",
    "special_tokens_map.json": "06e405a36dfe4b9604f484f6a1e619af1a7f7d09e34a8555eb0b77b66318067f",
    "tokenizer.json": "b60b6b43406a48bf3638526314f3d232d97058bc93472ff2de930d43686fa441",
    "tokenizer_config.json": "3f5961b9ac86288cccdb97f32fb848d6187c78e1603958c53f3ea1f296b7d8a2",
    "unigram.json": "71b44701d7efd054205115acfa6ef126c5d2f84bd3affe0c59e48163674d19a6",
}


class LocalMiniLM:
    space = EmbeddingSpace("Xenova/paraphrase-multilingual-MiniLM-L12-v2",
        "2c4055b12046f11709e9df2c122e59ffbdc2f900", 384,
        hashlib.sha256((_json(_FILES) + ":transformers-4.2.0:ort-1.24.3").encode()).hexdigest())

    def __init__(self, *, node: Path, transformers_dir: Path, model_dir: Path):
        if not node.is_absolute() or not transformers_dir.is_absolute():
            raise MemoryFailure("invalid_embedding_config")
        self.node = node.resolve()
        self.transformers_dir = transformers_dir.resolve()
        self.model_dir = external(model_dir)

    @classmethod
    def from_file(cls, path: Path) -> LocalMiniLM:
        config = _load(external(path))
        if (set(config) != {"schema_version", "node", "transformers_dir", "model_dir"}
                or type(config["schema_version"]) is not int or config["schema_version"] != 1
                or any(not isinstance(config[key], str) for key in ("node", "transformers_dir", "model_dir"))):
            raise MemoryFailure("invalid_embedding_config")
        return cls(node=Path(config["node"]), transformers_dir=Path(config["transformers_dir"]), model_dir=Path(config["model_dir"]))

    def embed(self, texts: tuple[str, ...], *, timeout: float) -> tuple[tuple[float, ...], ...]:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120
                or not isinstance(texts, tuple) or not 1 <= len(texts) <= 16):
            raise MemoryFailure("invalid_embedding_request")
        for text in texts:
            if not isinstance(text, str) or not text or len(text) > 256 or credential_signal(text):
                raise MemoryFailure("protected_embedding_input")
        try:
            # Check every invocation: modified model files cannot silently share a vector space.
            for name, expected in _FILES.items():
                path = (self.model_dir / name).resolve()
                if not path.is_relative_to(self.model_dir):
                    raise MemoryFailure("embedding_unavailable")
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                        raise MemoryFailure("embedding_integrity_mismatch")
            result = subprocess.run([str(self.node), str(Path(__file__).with_name("embedding_worker.mjs"))],
                input=_json({"model_dir": str(self.model_dir), "transformers_dir": str(self.transformers_dir), "texts": texts}).encode(),
                capture_output=True, timeout=timeout)
            if result.returncode or len(result.stdout) > 1024 * 1024:
                raise MemoryFailure("embedding_unavailable")
            value = json.loads(result.stdout.decode("utf-8", errors="strict"))
            if not isinstance(value, dict) or set(value) != {"vectors", "network_attempts"} or value["network_attempts"] != 0:
                raise MemoryFailure("embedding_unavailable")
            vectors = value["vectors"]
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise MemoryFailure("invalid_embedding")
            return tuple(unit_vector(vector, self.space.dimensions) for vector in vectors)
        except subprocess.TimeoutExpired:
            raise MemoryFailure("embedding_timeout") from None
        except (OSError, ValueError, TypeError):
            raise MemoryFailure("embedding_unavailable") from None
