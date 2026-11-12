"""Pinned experimental local reflection model; no downloader or remote provider."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from .extraction import LocalQwen, _FILES
from .models import MemoryFailure
from .paths import external
from .reflection import ModelTurn, ReflectionBudget, ReflectionEngine, ReflectionPolicy
from .runtime import MemoryRuntime, _json, _load, _unique_object
from .semantic import SemanticRecall


class LocalReflectionModel:
    def __init__(self, *, node: Path, transformers_dir: Path, model_dir: Path):
        self.assets = LocalQwen(node=node, transformers_dir=transformers_dir, model_dir=model_dir)
        self.worker = Path(__file__).with_name('reflection_worker.mjs')
        self.worker_digest = hashlib.sha256(self.worker.read_bytes()).hexdigest()
        self.processor_version = 'qwen3-0.6b-reflection-experimental-v1:' + hashlib.sha256(
            _json([self.assets.processor_version, self.worker_digest]).encode()).hexdigest()

    def turn(self, payload: dict[str, Any], *, input_tokens: int, output_tokens: int,
             timeout: float, cancel: threading.Event | None) -> ModelTurn:
        deadline = time.monotonic() + timeout
        def remaining() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('reflection_canceled')
            left = deadline - time.monotonic()
            if left <= 0:
                raise MemoryFailure('reflection_timeout')
            return left
        remaining()
        process: subprocess.Popen[bytes] | None = None
        try:
            for name, expected in _FILES.items():
                remaining()
                path = (self.assets.model_dir / name).resolve()
                if not path.is_relative_to(self.assets.model_dir):
                    raise MemoryFailure('reflection_integrity_mismatch')
                with path.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != expected:
                        raise MemoryFailure('reflection_integrity_mismatch')
            if hashlib.sha256(self.worker.read_bytes()).hexdigest() != self.worker_digest:
                raise MemoryFailure('reflection_integrity_mismatch')
            request = _json({'model_dir': str(self.assets.model_dir), 'transformers_dir': str(self.assets.transformers_dir),
                'payload': payload, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).encode()
            if len(request) > 262144:
                raise MemoryFailure('reflection_input_budget')
            remaining()
            process = subprocess.Popen([str(self.assets.node), str(self.worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            pending: bytes | None = request
            while True:
                try:
                    output, _ = process.communicate(input=pending, timeout=min(.1, remaining()))
                    break
                except subprocess.TimeoutExpired:
                    pending = None
            if process.returncode or len(output) > 131072:
                raise MemoryFailure('reflection_model_unavailable')
            value = json.loads(output.decode('utf-8', errors='strict'), object_pairs_hook=_unique_object)
            if (not isinstance(value, dict) or set(value) != {'action_json', 'input_tokens', 'output_tokens', 'network_attempts', 'status'}
                    or type(value['network_attempts']) is not int or value['network_attempts'] != 0):
                raise MemoryFailure('reflection_invalid_output')
            if value['status'] == 'input_budget':
                raise MemoryFailure('reflection_input_budget')
            if value['status'] != 'ok':
                raise MemoryFailure('reflection_invalid_output')
            try:
                action = LocalQwen.parse_selection(value['action_json'])
            except MemoryFailure:
                action = {'action': 'invalid_model_json'}
            if not isinstance(action, dict):
                action = {'action': 'invalid_model_json'}
            return ModelTurn(action, value['input_tokens'], value['output_tokens'])
        except (OSError, ValueError, TypeError, KeyError):
            raise MemoryFailure('reflection_model_unavailable') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()


def from_file(runtime: MemoryRuntime, recall: SemanticRecall, path: Path) -> ReflectionEngine:
    value = _load(external(path))
    fields = {'schema_version', 'profile', 'node', 'transformers_dir', 'model_dir', 'policy'}
    if (set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['profile'] != 'qwen3-0.6b-experimental-reflection-v1' or not isinstance(value['policy'], dict)
            or any(not isinstance(value[key], str) for key in ('node', 'transformers_dir', 'model_dir'))):
        raise MemoryFailure('invalid_reflection_config')
    try:
        policy = {**asdict(ReflectionPolicy()), **value['policy']}
        policy['normal'], policy['deep'] = ReflectionBudget(**policy['normal']), ReflectionBudget(**policy['deep'])
        return ReflectionEngine(runtime, recall, LocalReflectionModel(node=Path(value['node']), transformers_dir=Path(value['transformers_dir']),
            model_dir=Path(value['model_dir'])), ReflectionPolicy(**policy))
    except (TypeError, ValueError):
        raise MemoryFailure('invalid_reflection_config') from None
