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
            "--config", str(self.config), "--maintenance-access", str(self.access_path),
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
        with patch("evernook.embedding.LocalMiniLM.from_file", return_value=backend):
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
        with patch("evernook.embedding.LocalMiniLM.from_file", return_value=backend):
            maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
                embedding_config=self.config.parent / "synthetic-embedding.json")
        self.assertEqual(maintenance.tick()["skipped"], "foreground")
        self.assertEqual(SemanticRecall(self.runtime, backend).status(self.access).stored_vectors, 0)

    def test_embedding_failure_does_not_starve_independent_consolidation(self) -> None:
        self.runtime.submit(self.access, Event(Source("test", "semantic-error", "main", "1"),
            "user", 1, None, "complete", "合成：辛辣"))
        backend = SyntheticEmbedding()
        backend.error = "embedding_timeout"
        with patch("evernook.embedding.LocalMiniLM.from_file", return_value=backend):
            maintenance = ServiceMaintenance(self.runtime, self.access_path, 1, (), threading.Event(),
                embedding_config=self.config.parent / "synthetic-embedding.json")
        result = maintenance.tick()
        self.assertEqual(result["semantic"]["error_code"], "embedding_timeout")
        self.assertEqual(result["scheduler"]["processed"], 1)
