from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evernook.embedding import LocalMiniLM, ServiceEmbedding
from evernook.models import MemoryFailure


class EmbeddingSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-embedding-session-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        model = self.root / 'synthetic.onnx'
        model.write_bytes(b'synthetic model integrity fixture')
        self.assets = {'synthetic.onnx': hashlib.sha256(model.read_bytes()).hexdigest()}
        self.backend = LocalMiniLM(node=Path(sys.executable), transformers_dir=self.root, model_dir=self.root)
        worker = self.root / 'synthetic_worker.py'
        worker.write_text('''import json,os,pathlib,sys,time
for line in sys.stdin:
    value=json.loads(line)
    path=pathlib.Path(value['model_dir'])/'starts.jsonl'
    if not path.exists() or str(os.getpid()) not in path.read_text().splitlines():
        with path.open('a') as out: out.write(str(os.getpid())+'\\n')
    if value['texts'][0]=='delay': time.sleep(20)
    if value['texts'][0]=='hold-first': time.sleep(.6)
    if value['texts'][0]=='crash': raise SystemExit(7)
    if value['texts'][0]=='oversize': print('x'*1048577,flush=True); continue
    vectors=[]
    for text in value['texts']:
        vector=[0.0]*384; vector[0 if text=='first' else 1]=10**400 if text=='overflow' else 1.0; vectors.append(vector)
    print(json.dumps({'id':value['id']+(1 if value['texts'][0]=='bad-id' else 0),
        'vectors':vectors,'network_attempts':0}),flush=True)
''', encoding='utf8')
        self.addCleanup(patch.stopall)
        patch('evernook.embedding._FILES', self.assets).start()
        patch('evernook.embedding_session._WORKER', worker).start()

    def test_two_calls_share_worker_and_keep_order_and_space(self) -> None:
        with self.backend.session(timeout=10) as session:
            first = session.embed(('first', 'second'), timeout=5)
            second = session.embed(('second', 'first'), timeout=5)
            self.assertEqual(first, tuple(reversed(second)))
            self.assertEqual(session.space, self.backend.space)
            self.assertEqual(len((self.root / 'starts.jsonl').read_text().splitlines()), 1)
            pid = int((self.root / 'starts.jsonl').read_text().strip())
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel.OpenProcess(0x100000, False, pid)
            self.assertTrue(handle)
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 2000), 0)
        finally:
            kernel.CloseHandle(handle)
        with self.assertRaisesRegex(MemoryFailure, 'embedding_unavailable'):
            session.embed(('first',), timeout=1)

    def test_model_tamper_and_protected_input_never_reach_worker(self) -> None:
        with self.backend.session(timeout=10) as session:
            session.embed(('first',), timeout=5)
            (self.root / 'synthetic.onnx').write_bytes(b'changed')
            with self.assertRaisesRegex(MemoryFailure, 'embedding_integrity_mismatch'):
                session.embed(('second',), timeout=2)
        with self.backend.session(timeout=10) as session:
            with self.assertRaisesRegex(MemoryFailure, 'protected_embedding_input'):
                session.embed(('password=synthetic-only-123',), timeout=2)

    def test_timeout_and_inflight_cancellation_reap_owned_worker(self) -> None:
        for canceled in (False, True):
            with self.subTest(canceled=canceled), self.backend.session(timeout=10) as session:
                cancel = threading.Event()
                timer = threading.Timer(.5, cancel.set)
                if canceled:
                    timer.start()
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(MemoryFailure, 'embedding_canceled' if canceled else 'embedding_timeout'):
                        session.embed(('delay',), timeout=4 if canceled else .7, cancel=cancel)
                finally:
                    timer.cancel()
                self.assertLess(time.monotonic() - started, 4)

    def test_bad_reply_oversize_and_crash_fail_closed(self) -> None:
        for text in ('bad-id', 'oversize', 'crash', 'overflow'):
            with self.subTest(text=text), self.backend.session(timeout=10) as session:
                with self.assertRaises(MemoryFailure):
                    session.embed((text,), timeout=5)
                with self.assertRaisesRegex(MemoryFailure, 'embedding_unavailable'):
                    session.embed(('first',), timeout=1)

    def test_waiting_cancellation_does_not_kill_another_request(self) -> None:
        with self.backend.session(timeout=10) as session, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(session.embed, ('hold-first',), timeout=5)
            deadline = time.monotonic() + 3
            while not (self.root / 'starts.jsonl').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(first.done())
            cancel = threading.Event()
            timer = threading.Timer(.1, cancel.set); timer.start()
            try:
                with self.assertRaisesRegex(MemoryFailure, 'embedding_canceled'):
                    session.embed(('second',), timeout=2, cancel=cancel)
            finally:
                timer.cancel()
            self.assertIsNotNone(first.result(timeout=6)[0])
            self.assertIsNotNone(session.embed(('second',), timeout=2)[0])

    def test_service_binding_reuses_rotates_and_closes_its_own_worker(self) -> None:
        config = self.root / 'embedding.json'
        config.write_text(json.dumps({'schema_version': 1, 'node': str(self.backend.node),
            'model_dir': str(self.root), 'transformers_dir': str(self.root)}), encoding='utf8')
        binding = ServiceEmbedding(config)
        self.addCleanup(binding.close)
        backend = binding.current()
        self.assertIsInstance(backend, LocalMiniLM)
        backend.embed(('first',), timeout=5)
        backend.embed(('second',), timeout=5)
        self.assertEqual(len((self.root / 'starts.jsonl').read_text().splitlines()), 1)
        assert isinstance(backend, LocalMiniLM) and backend._managed is not None
        assert backend._managed.session is not None
        backend._managed.session.expires_at = time.monotonic() - 1
        backend.embed(('first',), timeout=5)
        self.assertEqual(len((self.root / 'starts.jsonl').read_text().splitlines()), 2)
        binding.close()
        with self.assertRaisesRegex(MemoryFailure, 'embedding_unavailable'):
            backend.embed(('first',), timeout=1)

    def test_abrupt_owner_death_reaps_real_descendant_handle(self) -> None:
        code = '''import hashlib,json,pathlib,sys,time
import evernook.embedding as embedding
import evernook.embedding_session as sessions
root=pathlib.Path(sys.argv[1])
embedding._FILES={'synthetic.onnx':hashlib.sha256((root/'synthetic.onnx').read_bytes()).hexdigest()}
sessions._WORKER=root/'synthetic_worker.py'
backend=embedding.LocalMiniLM(node=pathlib.Path(sys.executable),transformers_dir=root,model_dir=root)
with backend.session(timeout=30) as session:
    session.embed(('first',),timeout=5)
    print((root/'starts.jsonl').read_text().strip(),flush=True)
    time.sleep(25)
'''
        owner = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = None
        try:
            assert owner.stdout is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(owner.stdout.readline)
                try:
                    pid = int(future.result(timeout=8))
                except BaseException:
                    owner.kill()
                    raise
            handle = kernel.OpenProcess(0x100000, False, pid)
            self.assertTrue(handle)
            owner.kill()
            owner.wait(timeout=3)
            self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=3)
            if owner.stdout:
                owner.stdout.close()
            if handle:
                kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
