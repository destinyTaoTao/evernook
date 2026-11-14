from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize, pair_client
from evernook.service import ServiceClient, dispatch


class ServiceExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='evernook-service-extraction-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.owner = initialize(self.root / 'data', client_id='owner', entry='owner')
        self.owner_path = self.config.parent / 'mcp-access-owner.json'
        pair_client(self.config, client_id='owner', entry='owner', access_path=self.owner_path)
        self.ordinary = pair_client(self.config, client_id='ordinary', entry='ordinary',
            access_path=self.config.parent / 'mcp-access-ordinary.json', scopes=('capture', 'recall'))
        self.runtime = MemoryRuntime(self.config)
        self.source = self.runtime.submit(self.owner, Event(Source('owner', 'synthetic', 'main', '1'),
            'user', 1, None, 'complete', '合成：我喜欢摄影。')).message_id
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            if process.stderr:
                process.stderr.close()

    def start(self, profile: Path | None = None, *, activate: bool = False) -> ServiceClient:
        endpoint = self.config.parent / 'memory-service.json'
        old = endpoint.read_bytes() if endpoint.exists() else None
        args = [sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config)]
        if activate:
            args += ['--activate-owner', str(self.owner_path)]
        if profile:
            args += ['--extraction-config', str(profile)]
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('isolated service failed to start: ' + (process.stderr.read().decode() if process.stderr else ''))
            if endpoint.exists() and endpoint.read_bytes() != old:
                client = ServiceClient(self.config)
                client.call(self.owner, 'status', {})
                return client
            time.sleep(0.05)
        self.fail('isolated service readiness timeout')

    def test_service_owned_configuration_cli_authorization_and_no_model_tool(self) -> None:
        client = self.start(activate=True)
        arguments = {'command': 'extract', 'input': {'source_ids': [self.source]}}
        with self.assertRaisesRegex(MemoryFailure, 'extraction_service_unconfigured'):
            client.call(self.owner, 'cli', arguments)
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            client.call(self.ordinary, 'cli', arguments)
        self.processes[-1].terminate()
        self.processes[-1].wait(timeout=15)

        profile = self.root / 'extraction.json'
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'local-qwen',
            'profile': 'qwen3-0.6b-experimental-extractive-v1', 'node': sys.executable,
            'transformers_dir': str(self.root / 'missing-packages'), 'model_dir': str(self.root / 'missing-model')}), encoding='utf-8')
        client = self.start(profile)
        for command in ('extract', 'extract-preferences'):
            result = client.call(self.owner, 'cli', {'command': command, 'input': {'source_ids': [self.source]}})
            self.assertEqual(result['run']['state'], 'failed')
            self.assertEqual(result['error_code'], 'extraction_unavailable')
            with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                client.call(self.ordinary, 'cli', {'command': command, 'input': {'source_ids': [self.source]}})
            with self.assertRaisesRegex(MemoryFailure, 'invalid_arguments'):
                client.call(self.owner, 'cli', {'command': command, 'input': {'source_ids': [self.source], 'extraction_config': str(profile)}})
            cli = [sys.executable, '-m', 'evernook.cli', command, '--config', str(self.config),
                   '--access', str(self.owner_path), '--require-service']
            payload = json.dumps({'source_ids': [self.source]}).encode()
            response = json.loads(subprocess.run(cli, input=payload, capture_output=True, timeout=30).stdout)
            self.assertTrue(response['ok'], response)
            self.assertEqual(response['result']['error_code'], 'extraction_unavailable')
            rejected = json.loads(subprocess.run(cli + ['--extraction-config', str(profile)], input=payload,
                                                 capture_output=True, timeout=30).stdout)
            self.assertEqual(rejected['error']['code'], 'service_policy_mismatch')
        tools = client.call(self.owner, 'mcp-list', {'profile': 'trusted-host'})['tools']
        self.assertFalse(any('extract' in tool['name'] for tool in tools))
        self.assertEqual(client.call(self.owner, 'read', {'message_id': self.source})['event']['text'], '合成：我喜欢摄影。')

    def test_invalid_startup_provider_does_not_activate_or_publish_endpoint(self) -> None:
        profile = self.root / 'invalid-provider.json'
        profile.write_text(json.dumps({'schema_version': 2, 'provider': 'unknown'}), encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', '-m', 'evernook.service', '--config', str(self.config),
            '--activate-owner', str(self.owner_path), '--extraction-config', str(profile)], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'invalid_extraction_config', result.stderr)
        self.assertNotIn(str(profile).encode(), result.stderr)
        self.assertFalse((self.config.parent / 'memory-service.json').exists())
        self.assertIsNot(json.loads(self.config.read_text(encoding='utf-8')).get('service_mode'), True)

    def test_service_gate_and_cancellation_preserve_candidate_boundary(self) -> None:
        class Selector:
            processor_version = 'synthetic-service-selector-v1'
            def select(self, sources: object, *, timeout: float, cancel: threading.Event | None) -> object:
                raise AssertionError('canceled request must not run a model')
        gate = threading.BoundedSemaphore(1)
        request = {'version': 1, 'access': asdict(self.owner), 'operation': 'cli',
                   'arguments': {'command': 'extract', 'input': {'source_ids': [self.source]}}}
        gate.acquire()
        try:
            with self.assertRaisesRegex(MemoryFailure, 'extraction_service_busy'):
                dispatch(self.runtime, request, extractor=Selector(), extraction_gate=gate)  # type: ignore[arg-type]
        finally:
            gate.release()
        cancel = threading.Event(); cancel.set()
        result = dispatch(self.runtime, request, cancel=cancel, extractor=Selector(), extraction_gate=gate)  # type: ignore[arg-type]
        self.assertEqual(result.error_code, 'extraction_canceled')  # type: ignore[union-attr]
        self.assertTrue(gate.acquire(blocking=False))
        gate.release()
        protected = self.runtime.submit(self.owner, Event(Source('owner', 'synthetic', 'main', '2'),
            'user', 2, None, 'complete', '合成私密材料', sensitive=True)).message_id
        request['arguments']['input']['source_ids'] = [protected]  # type: ignore[index]
        result = dispatch(self.runtime, request, extractor=Selector(), extraction_gate=gate)  # type: ignore[arg-type]
        self.assertEqual(result.error_code, 'extraction_input_not_admitted')  # type: ignore[union-attr]

    def test_real_disconnect_cancels_worker_and_releases_service_gate(self) -> None:
        marker = self.root / 'worker-state'
        worker = Path(__file__).with_name('service_extraction_worker.py')
        process = subprocess.Popen([sys.executable, '-B', str(worker), str(self.config), str(self.owner_path), str(marker)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        client = ServiceClient(self.config)
        deadline = time.monotonic() + 20
        while not (self.config.parent / 'memory-service.json').exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail('synthetic blocking service failed to start')
            time.sleep(0.05)
        canceled = threading.Event()
        errors: list[str] = []
        def run() -> None:
            try:
                client.call(self.owner, 'cli', {'command': 'extract', 'input': {'source_ids': [self.source], 'timeout': 15}},
                            timeout=20, cancel=canceled)
            except MemoryFailure as error:
                errors.append(error.code)
        caller = threading.Thread(target=run)
        caller.start()
        self.addCleanup(lambda: caller.join(timeout=20))
        deadline = time.monotonic() + 15
        while not marker.exists():
            if not caller.is_alive() or time.monotonic() >= deadline:
                self.fail(f'blocking backend did not start: {errors}')
            time.sleep(0.05)
        with self.assertRaisesRegex(MemoryFailure, 'extraction_service_busy'):
            client.call(self.owner, 'cli', {'command': 'extract-preferences', 'input': {'source_ids': [self.source]}})
        canceled.set(); caller.join(timeout=10)
        self.assertFalse(caller.is_alive())
        self.assertEqual(errors, ['service_canceled'])
        deadline = time.monotonic() + 10
        while marker.read_text(encoding='utf-8') != 'canceled':
            if time.monotonic() >= deadline:
                self.fail('service did not cancel its backend after client disconnect')
            time.sleep(0.05)
        # The marker is written before durable failure cleanup, so allow that cleanup to finish.
        deadline = time.monotonic() + 10
        while True:
            try:
                client.call(self.owner, 'cli', {'command': 'extract', 'input': {'source_ids': ['missing-synthetic']}})
                self.fail('missing source unexpectedly accepted')
            except MemoryFailure as error:
                if error.code != 'extraction_service_busy':
                    self.assertEqual(error.code, 'message_not_found')
                    break
                if time.monotonic() >= deadline:
                    self.fail('extraction gate remained busy after cancellation')
                time.sleep(0.05)


if __name__ == '__main__':
    unittest.main()
