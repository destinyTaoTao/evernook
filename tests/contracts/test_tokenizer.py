from __future__ import annotations

import hashlib
import ctypes
from ctypes import wintypes
import json
import os
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

    def test_scoped_counter_reuses_real_worker_and_keeps_integrity_checks(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        counter = LocalTokenCounter.from_file(self.config)
        children: list[subprocess.Popen[str]] = []
        original = subprocess.Popen
        def spawn(arguments: list[str], **options: Any) -> subprocess.Popen[str]:
            child: subprocess.Popen[str] = original(arguments, **options)
            children.append(child)
            return child
        with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn):
            with counter.session(timeout=2) as scoped:
                self.assertEqual(scoped.counter_id, counter.counter_id)
                self.assertEqual(scoped.count("hello world"), 2)
                self.assertEqual(scoped.count("你好"), 1)
                self.assertEqual(scoped.count(""), 0)
                self.assertEqual(len(children), 1)
                self.assertIsNone(children[0].poll())
                self.tokenizer.write_text('{}', encoding="utf-8")
                with self.assertRaises(MemoryFailure):
                    scoped.count("hello")
            self.assertIsNotNone(children[0].poll())

    def test_cancellation_reaps_the_running_local_counter_child(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        counter = LocalTokenCounter.from_file(self.config)
        counter.cancel = threading.Event()
        children: list[subprocess.Popen[str]] = []
        original = subprocess.Popen
        cancel = counter.cancel
        def spawn(_arguments: object, **options: Any) -> subprocess.Popen[str]:
            child = original([str(counter.node), "-e", "process.stdin.resume(); setInterval(()=>{},1000)"], **options)
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

    def test_scoped_timeout_and_cancellation_reap_a_nonresponsive_worker(self) -> None:
        from evernook.tokenizer import LocalTokenCounter

        for canceling in (False, True):
            counter = LocalTokenCounter.from_file(self.config)
            flag = threading.Event()
            counter.cancel = flag if canceling else None
            children: list[subprocess.Popen[str]] = []
            original = subprocess.Popen
            def spawn(_arguments: object, **options: Any) -> subprocess.Popen[str]:
                child: subprocess.Popen[str] = original(
                    [str(counter.node), "-e", "process.stdin.resume(); setInterval(()=>{},1000)"], **options)
                children.append(child)
                return child
            timer = threading.Timer(.05, flag.set)
            with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn):
                if canceling:
                    timer.start()
                try:
                    with counter.session(timeout=.2 if not canceling else 2) as scoped:
                        with self.assertRaisesRegex(MemoryFailure, "context_canceled" if canceling else "tokenizer_unavailable"):
                            scoped.count("hello")
                finally:
                    timer.cancel()
                    if canceling:
                        timer.join()
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())

    def test_host_build_owns_one_worker_and_releases_it_before_returning(self) -> None:
        from evernook import Event, MemoryRuntime, Source, initialize
        from evernook.host_context import HostContext
        from evernook.semantic import SemanticRecall
        from evernook.tokenizer import LocalTokenCounter

        config, access = initialize(self.root / "owned-runtime", client_id="test", entry="test")
        runtime = MemoryRuntime(config)
        for number in range(6):
            runtime.submit(access, Event(Source("test", "synthetic", "main", str(number)),
                "user", number, None, "complete", "hello world " * 8))
        context_file = self.root / "owned-context.json"
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config),
            "policy": {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 0,
                       "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
        host = HostContext(runtime, SemanticRecall(runtime), context_file)
        children: list[subprocess.Popen[str]] = []
        original = subprocess.Popen
        def spawn(arguments: list[str], **options: Any) -> subprocess.Popen[str]:
            child: subprocess.Popen[str] = original(arguments, **options)
            children.append(child)
            return child
        with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn):
            result = host.build(access, "hello", model_id="synthetic-local", max_tokens=256,
                as_of="2026-11-04T00:00:00Z", mode="normal", timeout=5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(LocalTokenCounter.from_file(self.config).count(result.payload), result.used_tokens)

    def test_owner_process_exit_closes_the_scoped_worker_input(self) -> None:
        parent = self.root / "owner.py"
        parent.write_text('''import os,sys,subprocess
from unittest.mock import patch
from pathlib import Path
from evernook.tokenizer import LocalTokenCounter
original=subprocess.Popen
children=[]
def spawn(*args,**kwargs):
 child=original(*args,**kwargs); children.append(child.pid); return child
with patch('evernook.tokenizer.subprocess.Popen',side_effect=spawn):
 with LocalTokenCounter.from_file(Path(sys.argv[1])).session(timeout=2) as scoped:
  assert scoped.count('hello')==1
  print(children[0],flush=True)
  os._exit(0)
''', encoding="utf-8")
        result = subprocess.run([sys.executable, str(parent), str(self.config)], capture_output=True,
            text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")))
        self.assertEqual(result.returncode, 0, result.stderr)
        worker_pid = int(result.stdout.strip())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, worker_pid)
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 2000), 0)
            finally:
                kernel.CloseHandle(handle)
        else:
            self.assertEqual(ctypes.get_last_error(), 87)  # Already exited: invalid process ID.

    def test_host_prewarm_overlaps_recall_and_rechecks_assets_in_the_same_child(self) -> None:
        from evernook import MemoryRuntime, initialize
        from evernook.host_context import HostContext
        from evernook.semantic import RecallPage, SemanticRecall
        from evernook.tokenizer import LocalTokenCounter, _TokenSession

        for tamper in (False, True):
            with self.subTest(tamper=tamper):
                config, access = initialize(self.root / f"warm-{tamper}", client_id="test", entry="test")
                runtime = MemoryRuntime(config)
                context_file = self.root / f"warm-context-{tamper}.json"
                context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config),
                    "policy": {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 0,
                               "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
                host = HostContext(runtime, SemanticRecall(runtime), context_file)
                ready = threading.Event()
                children: list[subprocess.Popen[str]] = []
                original_popen, original_count = subprocess.Popen, _TokenSession.count
                original_recall = host.recall.recall

                def counted(session: _TokenSession, text: str) -> int:
                    result = original_count(session, text)
                    if text == "":
                        self.assertEqual(result, 0)
                        ready.set()
                    return result

                def recall(*args: Any, **kwargs: Any) -> RecallPage:
                    self.assertTrue(ready.wait(2), "tokenizer must initialize while recall is in progress")
                    if tamper:
                        self.model_config.write_text('{}\n', encoding="utf-8")
                    return original_recall(*args, **kwargs)

                def spawn(arguments: list[str], **options: Any) -> subprocess.Popen[str]:
                    child: subprocess.Popen[str] = original_popen(arguments, **options)
                    children.append(child)
                    return child

                with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn), \
                        patch.object(_TokenSession, "count", counted), patch.object(host.recall, "recall", recall):
                    if tamper:
                        with self.assertRaisesRegex(MemoryFailure, "tokenizer_unavailable"):
                            host.build(access, "hello", model_id="synthetic-local", max_tokens=512,
                                as_of="2026-11-04T00:00:00Z", mode="normal", timeout=5)
                    else:
                        bundle = host.build(access, "hello", model_id="synthetic-local", max_tokens=512,
                            as_of="2026-11-04T00:00:00Z", mode="normal", timeout=5)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                if not tamper:
                    self.assertEqual(LocalTokenCounter.from_file(self.config).count(bundle.payload), bundle.used_tokens)

    def test_invalid_host_request_reaps_nonresponsive_prewarm_before_returning(self) -> None:
        from evernook import MemoryRuntime, initialize
        from evernook.context import ContextBuilder, ContextBundle
        from evernook.host_context import HostContext
        from evernook.semantic import SemanticRecall

        config, access = initialize(self.root / "invalid-warm", client_id="test", entry="test")
        runtime = MemoryRuntime(config)
        context_file = self.root / "invalid-warm-context.json"
        context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config),
            "policy": {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 0,
                       "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
        host = HostContext(runtime, SemanticRecall(runtime), context_file)
        children: list[subprocess.Popen[str]] = []
        original_popen, original_build = subprocess.Popen, ContextBuilder.build
        spawned = threading.Event()

        def spawn(arguments: list[str], **options: Any) -> subprocess.Popen[str]:
            child: subprocess.Popen[str] = original_popen(
                [arguments[0], "-e", "process.stdin.resume(); setInterval(()=>{},1000)"], **options)
            children.append(child)
            spawned.set()
            return child

        def build(builder: ContextBuilder, *args: Any, **kwargs: Any) -> ContextBundle:
            self.assertTrue(spawned.wait(2))
            return original_build(builder, *args, **kwargs)

        with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn), patch.object(ContextBuilder, "build", build):
            with self.assertRaisesRegex(MemoryFailure, "protected_query"):
                host.build(access, "password=synthetic-protected-123", model_id="synthetic-local", max_tokens=512,
                    as_of="2026-11-04T00:00:00Z", mode="normal", timeout=10)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(any(thread.name.startswith("evernook-tokenizer") for thread in threading.enumerate()))

    def test_host_cancel_and_timeout_reap_pending_prewarm_and_count(self) -> None:
        from evernook import MemoryRuntime, initialize
        from evernook.host_context import HostContext
        from evernook.semantic import SemanticRecall

        for canceling in (False, True):
            with self.subTest(canceling=canceling):
                config, access = initialize(self.root / f"stalled-warm-{canceling}", client_id="test", entry="test")
                runtime = MemoryRuntime(config)
                context_file = self.root / f"stalled-warm-context-{canceling}.json"
                context_file.write_text(json.dumps({"schema_version": 1, "tokenizer_config": str(self.config),
                    "policy": {"normal_tokens": 512, "deep_tokens": 1024, "pinned_tokens": 0,
                               "recent_days": 7, "recent_target_chars": 8192}}), encoding="utf-8")
                host = HostContext(runtime, SemanticRecall(runtime), context_file)
                canceled = threading.Event()
                timer = threading.Timer(.05, canceled.set)
                children: list[subprocess.Popen[str]] = []
                original_popen = subprocess.Popen

                def spawn(arguments: list[str], **options: Any) -> subprocess.Popen[str]:
                    child: subprocess.Popen[str] = original_popen(
                        [arguments[0], "-e", "process.stdin.resume(); setInterval(()=>{},1000)"], **options)
                    children.append(child)
                    if canceling:
                        timer.start()
                    return child

                with patch("evernook.tokenizer.subprocess.Popen", side_effect=spawn):
                    try:
                        with self.assertRaisesRegex(MemoryFailure, "context_canceled" if canceling else "tokenizer_unavailable"):
                            host.build(access, "hello", model_id="synthetic-local", max_tokens=512,
                                as_of="2026-11-04T00:00:00Z", mode="normal", timeout=2 if canceling else .3,
                                cancel=canceled if canceling else None)
                    finally:
                        timer.cancel()
                        if canceling and children:
                            timer.join()
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertFalse(any(thread.name.startswith("evernook-tokenizer") for thread in threading.enumerate()))

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
