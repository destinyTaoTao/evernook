from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from evernook import MemoryFailure


class LocalTokenizerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-tokenizer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tokenizer = self.root / "tokenizer.json"
        self.tokenizer.write_text(json.dumps({"model": {"type": "WordLevel", "vocab": {"[UNK]": 0,
            "hello": 1, "world": 2, "你好": 3}, "unk_token": "[UNK]"}, "pre_tokenizer": {"type": "Whitespace"},
            "decoder": None, "normalizer": None, "post_processor": None, "added_tokens": []}), encoding="utf-8")
        self.model_config = self.root / "tokenizer_config.json"
        self.model_config.write_text('{}', encoding="utf-8")
        node = shutil.which("node")
        assert node is not None
        self.config = self.root / "counter.json"
        self.config.write_text(json.dumps({"schema_version": 1, "node": node,
            "package_dir": str(Path(__file__).resolve().parents[2] / "plugins/harnesses/node_modules/@huggingface/tokenizers"),
            "model_id": "synthetic-local", "tokenizer_file": str(self.tokenizer), "config_file": str(self.model_config),
            "tokenizer_sha256": hashlib.sha256(self.tokenizer.read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256(self.model_config.read_bytes()).hexdigest()}), encoding="utf-8")

    def test_real_local_tokenizer_counts_tokens_without_character_estimation(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        counter = LocalTokenCounter.from_file(self.config)
        self.assertEqual(counter.model_id, "synthetic-local")
        self.assertEqual(counter.count("hello world"), 2)
        self.assertEqual(counter.count("你好"), 1)
        self.assertEqual(counter.count(""), 0)

    def test_changed_tokenizer_assets_fail_instead_of_reusing_an_old_count(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        counter = LocalTokenCounter.from_file(self.config)
        self.assertEqual(counter.count("hello"), 1)
        self.tokenizer.write_text('{}', encoding="utf-8")
        with self.assertRaises(MemoryFailure):
            counter.count("hello")

    def test_cancellation_reaps_the_running_local_counter_child(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        counter = LocalTokenCounter.from_file(self.config)
        counter.cancel = threading.Event()
        children: list[subprocess.Popen[str]] = []
        original = subprocess.Popen
        cancel = counter.cancel
        def spawn(_arguments: object, **options: Any) -> subprocess.Popen[str]:
            child = original([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"], **options)
            children.append(child)
            timer = threading.Timer(0.2, cancel.set)
            self.addCleanup(timer.cancel)
            timer.start()
            return child
        with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn):
            with self.assertRaisesRegex(MemoryFailure, "context_canceled"):
                counter.count("hello")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_host_context_uses_saved_policy_model_binding_and_request_ceiling(self) -> None:
        from evernook import MemoryRuntime, initialize
        from evernook.host_context import HostContext
        from evernook.semantic import SemanticRecall

        config, access = initialize(self.root / "runtime", client_id="test", entry="test")
        runtime = MemoryRuntime(config)
        policy = {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 128,
                  "recent_days": 7, "recent_target_chars": 8192}
        context_file = self.root / "context.json"
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config), "policy": policy}), encoding="utf-8")
        host = HostContext(runtime, SemanticRecall(runtime), context_file)
        result = host.build(access, "hello", model_id="synthetic-local", max_tokens=256,
            as_of="2026-11-02T12:00:00+00:00", mode="normal", timeout=30)
        self.assertEqual(result.token_limit, 256)
        self.assertLessEqual(result.used_tokens, 256)
        self.assertEqual(host.status()["saved_policy"]["normal_tokens"], 512)
        policy["normal_tokens"] = 128
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config), "policy": policy}), encoding="utf-8")
        updated = host.build(access, "hello", model_id="synthetic-local", max_tokens=256,
            as_of="2026-11-02T12:00:00+00:00", mode="normal", timeout=30)
        self.assertEqual(updated.token_limit, 128)
        with self.assertRaisesRegex(MemoryFailure, "tokenizer_model_mismatch"):
            host.build(access, "hello", model_id="other", max_tokens=256,
                as_of="2026-11-02T12:00:00+00:00", mode="normal", timeout=30)
        invalid_policy = {**policy, "fact_keys": ["ab"]}
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config), "policy": invalid_policy}), encoding="utf-8")
        with self.assertRaisesRegex(MemoryFailure, "invalid_context_policy"):
            host.status()
