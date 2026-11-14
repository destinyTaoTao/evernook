from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
import threading
from unittest.mock import patch

from evernook import Event, MemoryRuntime, Source, initialize
from evernook.scheduler import ConsolidationScheduler
from evernook.service import ServiceClient
from evernook.service_maintenance import ServiceMaintenance
from evernook.semantic import SemanticRecall
from test_semantic import SyntheticEmbedding
from evernook.maintenance import NavigationMaintenance, NavigationPolicy


class ServiceMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="evernook-service-maintenance-")
        self.addCleanup(temporary.cleanup)
        self.config, self.access = initialize(Path(temporary.name) / "data",
            client_id="synthetic", entry="test")
        self.access_path = self.config.parent / "mcp-access-worker.json"
        self.access_path.write_text(json.dumps(asdict(self.access)), encoding="utf-8")
        self.runtime = MemoryRuntime(self.config)
        self.scheduler = ConsolidationScheduler(self.runtime)
        self.scheduler.configure(self.access, min_pending=1, idle_seconds=0, max_trigger_seconds=1)
        self.policy_id = NavigationMaintenance(self.runtime).configure(self.access, NavigationPolicy(480)).policy_id
        self.client = ServiceClient(self.config)
        self.process: subprocess.Popen[bytes] | None = None
        self.addCleanup(self.stop)

    def start(self) -> None:
        endpoint = self.config.parent / "memory-service.json"
        old = endpoint.read_bytes() if endpoint.exists() else None
        self.process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config), "--allow-legacy-shared", "--maintenance-access", str(self.access_path),
            "--maintenance-interval", "0.2", "--navigation-policy", self.policy_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while not endpoint.exists() or endpoint.read_bytes() == old:
            if self.process.poll() is not None:
                self.fail("maintenance service startup failed")
            if time.monotonic() > deadline:
                self.fail("maintenance service readiness timeout")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=15)
            if self.process.stderr:
                self.process.stderr.close()

    def save(self, number: int) -> None:
        event = Event(Source("test", "maintenance", "main", str(number)),
            "user", number, None, "complete", f"合成：独立后台消息{number}")
        self.client.call(self.access, "remember", {"event": asdict(event)})

    def wait_processed(self, count: int) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.client.call(self.access, "cli",
                {"command": "scheduler-status", "input": {}})
            if status["processed"] >= count:
                return dict(status)
            time.sleep(0.2)
        self.fail(f"background did not process {count}")

    def test_background_runs_without_clients_and_resumes_after_restart(self) -> None:
        self.start()
        self.save(1)
        del self.client
        time.sleep(1)
        self.client = ServiceClient(self.config)
        self.assertEqual(self.wait_processed(1)["processed"], 1)
        status = self.client.call(self.access, "status", {})
        self.assertTrue(status["service"]["maintenance"]["enabled"])
        self.assertEqual(status["lexical"]["state"], "ready")
        self.assertGreater(status["service"]["maintenance"]["navigation"][self.policy_id]["succeeded"], 0)
        self.stop()
        self.start()
        self.save(2)
        self.assertEqual(self.wait_processed(2)["processed"], 2)

    def test_foreground_and_pause_keep_work_pending(self) -> None:
        self.scheduler.pause(self.access, paused=True)
        self.start()
        self.save(1)
        time.sleep(0.6)
        status = self.client.call(self.access, "cli", {"command": "scheduler-status", "input": {}})
        self.assertEqual(status["processed"], 0)
        self.client.call(self.access, "mcp", {"name": "evernook_v1_host_activity", "profile": "trusted-host",
            "arguments": {"session": "foreground", "active": True, "ttl_seconds": 10}})
        self.client.call(self.access, "cli", {"command": "scheduler-pause", "input": {"paused": False}})
        time.sleep(0.6)
        status = self.client.call(self.access, "cli", {"command": "scheduler-status", "input": {}})
        self.assertEqual(status["processed"], 0)
        self.client.call(self.access, "mcp", {"name": "evernook_v1_host_activity", "profile": "trusted-host",
            "arguments": {"session": "foreground", "active": False, "ttl_seconds": 10}})
        self.assertEqual(self.wait_processed(1)["processed"], 1)

    def test_topic_views_refresh_in_independent_service_and_ordinary_clients_only_read(self) -> None:
        from evernook.models import MemoryFailure
        from evernook.runtime import pair_client
        self.start()
        definition = {'title': '后台消息', 'purpose': '合成后台验收', 'query': '后台消息', 'refresh_interval_seconds': 1}
        self.client.call(self.access, 'cli', {'command': 'topic-view-define', 'input': {
            'request_id': 'define-topic', 'view_id': 'background', 'expected_revision': 0, 'definition': definition}})
        self.save(1)
        ordinary = pair_client(self.config, client_id='topic-reader', entry='reader',
            access_path=self.config.parent / 'mcp-access-topic-reader.json', scopes=('capture', 'recall'))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.client.call(ordinary, 'cli', {'command': 'topic-view-read', 'input': {'view_id': 'background'}})
            if result['state'] == 'valid':
                break
            time.sleep(0.2)
        else:
            self.fail('topic background refresh did not publish a valid snapshot')
        self.assertIn('独立后台消息1', result['data']['citations'][0]['quote'])
        for name, arguments in (('topic-view-refresh', {'view_id': 'background'}),
                ('topic-view-control', {'request_id': 'pause', 'view_id': 'background', 'expected_revision': 1, 'action': 'pause'})):
            with self.subTest(command=name), self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                self.client.call(ordinary, 'cli', {'command': name, 'input': arguments})

    def test_optional_observation_candidates_continue_in_service_without_host(self) -> None:
        self.start()
        self.client.call(self.access, 'cli', {'command': 'observation-configure', 'input': {'enabled': True}})
        event = asdict(Event(Source('test', 'observation-first', 'main', '1'), 'user', 1, None, 'complete', '我喜欢摄影。'))
        receipt = self.client.call(self.access, 'remember', {'event': event})
        self.assertEqual(self.client.call(self.access, 'remember', {'event': event}), receipt)
        self.client.call(self.access, 'remember', {'event': asdict(Event(Source('test', 'observation-second', 'main', '1'),
            'user', 1, None, 'complete', '我不喜欢摄影。'))})
        deadline = time.monotonic() + 15
        while True:
            status = self.client.call(self.access, 'cli', {'command': 'observation-status', 'input': {}})
            if status['completed_runs'] >= 2:
                break
            if time.monotonic() > deadline:
                self.fail('independent observation maintenance did not finish')
            time.sleep(.1)
        from evernook.automatic_preferences import PreferenceUpdates
        key, _ = PreferenceUpdates._key_value('我喜欢摄影。', 'general_interest')
        related = self.client.call(self.access, 'cli', {'command': 'observation-related', 'input': {'topic_key': key}})
        row, = related['rows']
        self.assertEqual((row['independent_support'], row['independent_counterevidence']), (1, 1))
        self.assertEqual(row['revision'], 2)
        self.assertEqual(row['state'], 'candidate')
        published = self.client.call(self.access, 'cli', {'command': 'observation-query', 'input': {'topic_key': key}})
        self.assertEqual(published['rows'], [])

    def test_pause_during_indexing_stops_remaining_sources_and_consolidation(self) -> None:
        for number in range(3):
            self.runtime.submit(self.access, Event(Source("test", "mid-tick", "main", str(number)),
                "user", number, None, "complete", f"合成中途暂停{number}"))
        maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event())
        original = self.runtime.refresh_lexical
        def pause_after_one(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.scheduler.pause(self.access, paused=True)
            return result
        with patch.object(self.runtime, "refresh_lexical", side_effect=pause_after_one) as indexing:
            outcome = maintenance.tick()
        self.assertEqual(indexing.call_count, 1)
        self.assertEqual(outcome["skipped"], "paused")
        self.assertEqual(self.scheduler.status(self.access).processed, 0)

    def test_configured_embedding_is_maintained_without_a_host(self) -> None:
        self.runtime.submit(self.access, Event(Source("test", "semantic-idle", "main", "1"),
            "user", 1, None, "complete", "合成：辛辣"))
        backend = SyntheticEmbedding()
        with patch("evernook.service_maintenance.load_embedding", return_value=backend):
            maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
                embedding_config=self.config.parent / "synthetic-embedding.json")
        result = maintenance.tick()
        self.assertEqual(result["semantic"]["state"], "ready")
        self.assertEqual(SemanticRecall(self.runtime, backend).recall(self.access, "饮食偏好").hits[0].evidence.role, "user")

    def test_foreground_arriving_during_embedding_cancels_publication(self) -> None:
        self.runtime.submit(self.access, Event(Source("test", "semantic-stop", "main", "1"),
            "user", 1, None, "complete", "合成：辛辣"))
        backend = SyntheticEmbedding()
        backend.hook = lambda: self.scheduler.foreground(self.access, session="incoming", active=True, ttl_seconds=30)
        with patch("evernook.service_maintenance.load_embedding", return_value=backend):
            maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
                embedding_config=self.config.parent / "synthetic-embedding.json")
        self.assertEqual(maintenance.tick()["skipped"], "foreground")
        self.assertEqual(SemanticRecall(self.runtime, backend).status(self.access).stored_vectors, 0)

    def test_embedding_failure_does_not_starve_independent_consolidation(self) -> None:
        self.runtime.submit(self.access, Event(Source("test", "semantic-error", "main", "1"),
            "user", 1, None, "complete", "合成：辛辣"))
        backend = SyntheticEmbedding()
        backend.error = "embedding_timeout"
        with patch("evernook.service_maintenance.load_embedding", return_value=backend):
            maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
                embedding_config=self.config.parent / "synthetic-embedding.json")
        result = maintenance.tick()
        self.assertEqual(result["semantic"]["error_code"], "embedding_timeout")
        self.assertEqual(result["scheduler"]["processed"], 1)

    def test_changed_service_embedding_stops_semantic_and_topic_maintenance(self) -> None:
        from evernook.embedding import ServiceEmbedding
        self.runtime.submit(self.access, Event(Source('test', 'embedding-config', 'main', '1'),
            'user', 1, None, 'complete', '合成：后台消息'))
        path = self.config.parent.parent / 'embedding-config.json'
        value = {'schema_version': 2, 'provider': 'local-minilm', 'node': str(self.config.parent.parent / 'node'),
                 'transformers_dir': str(self.config.parent.parent / 'deps'), 'model_dir': str(self.config.parent.parent / 'a')}
        path.write_text(json.dumps(value), encoding='utf-8')
        binding = ServiceEmbedding(path)
        maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
            embedding_config=path, embedding_binding=binding)
        value['model_dir'] = str(self.config.parent.parent / 'b')
        path.write_text(json.dumps(value), encoding='utf-8')
        result = maintenance.tick()
        self.assertEqual(result['semantic']['error_code'], 'embedding_config_changed')
        self.assertEqual(result['topic_views']['error_code'], 'embedding_config_changed')
        self.assertEqual(result['scheduler']['processed'], 1)
        assert maintenance.semantic is not None
        self.assertEqual(maintenance.semantic.status(self.access).stored_vectors, 0)

    def test_invalid_startup_embedding_keeps_independent_maintenance_running(self) -> None:
        from evernook.embedding import ServiceEmbedding
        self.runtime.submit(self.access, Event(Source('test', 'invalid-embedding', 'main', '1'),
            'user', 1, None, 'complete', '合成：独立维护'))
        path = self.config.parent.parent / 'invalid-embedding.json'
        path.write_text('{', encoding='utf-8')
        binding = ServiceEmbedding(path)
        maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
            embedding_config=path, embedding_binding=binding)
        result = maintenance.tick()
        self.assertEqual(result['semantic']['error_code'], 'invalid_config')
        self.assertEqual(result['topic_views']['error_code'], 'invalid_config')
        self.assertEqual(result['scheduler']['processed'], 1)
