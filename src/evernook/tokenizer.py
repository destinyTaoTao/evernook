"""Explicit offline target-tokenizer counting; no character-count fallback."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

from .models import MemoryFailure
from .paths import external
from .runtime import _identifier, _json, _load


class LocalTokenCounter:
    def __init__(self, *, node: Path, package_dir: Path, model_id: str, tokenizer_file: Path,
                 config_file: Path, tokenizer_sha256: str, config_sha256: str):
        _identifier(model_id)
        if not node.is_absolute() or not package_dir.is_absolute():
            raise MemoryFailure("invalid_tokenizer_config")
        for value in (tokenizer_sha256, config_sha256):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise MemoryFailure("invalid_tokenizer_config")
        self.node, self.package_dir = node.resolve(), package_dir.resolve()
        self.model_id = model_id
        self.tokenizer_file, self.config_file = external(tokenizer_file), external(config_file)
        self.tokenizer_sha256, self.config_sha256 = tokenizer_sha256, config_sha256
        self.cancel: threading.Event | None = None
        self.counter_id = "local-tokenizer-v1-" + hashlib.sha256(
            _json([model_id, tokenizer_sha256, config_sha256, "tokenizers-js-0.2.0"]).encode()).hexdigest()

    @classmethod
    def from_file(cls, path: Path) -> LocalTokenCounter:
        config = _load(external(path))
        strings = {"node", "package_dir", "model_id", "tokenizer_file", "config_file", "tokenizer_sha256", "config_sha256"}
        if (set(config) != strings | {"schema_version"} or type(config["schema_version"]) is not int
                or config["schema_version"] != 1 or any(not isinstance(config[key], str) for key in strings)):
            raise MemoryFailure("invalid_tokenizer_config")
        return cls(node=Path(config["node"]), package_dir=Path(config["package_dir"]), model_id=config["model_id"],
            tokenizer_file=Path(config["tokenizer_file"]), config_file=Path(config["config_file"]),
            tokenizer_sha256=config["tokenizer_sha256"], config_sha256=config["config_sha256"])

    def count(self, text: str) -> int:
        if not isinstance(text, str) or len(text) > 1048576:
            raise MemoryFailure("invalid_tokenizer_request")
        payload = _json({"text": text, "package_dir": str(self.package_dir), "tokenizer_file": str(self.tokenizer_file),
            "config_file": str(self.config_file), "tokenizer_sha256": self.tokenizer_sha256, "config_sha256": self.config_sha256})
        try:
            if self.cancel is not None and self.cancel.is_set():
                raise MemoryFailure("context_canceled")
            with subprocess.Popen([str(self.node), str(Path(__file__).with_name("tokenizer_worker.mjs"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") as process:
                deadline = time.monotonic() + 10
                sent = False
                try:
                    while True:
                        if self.cancel is not None and self.cancel.is_set():
                            raise MemoryFailure("context_canceled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise MemoryFailure("tokenizer_unavailable")
                        try:
                            output, _ = process.communicate(None if sent else payload, timeout=min(0.1, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            sent = True
                    if process.returncode != 0:
                        raise MemoryFailure("tokenizer_unavailable")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            value = json.loads(output)
            if (not isinstance(value, dict) or set(value) != {"count"} or type(value["count"]) is not int
                    or value["count"] < 0 or (bool(text) and value["count"] == 0)):
                raise MemoryFailure("invalid_token_counter")
            return int(value["count"])
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            raise MemoryFailure("tokenizer_unavailable") from None
