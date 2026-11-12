from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import Event, Source, initialize
from evernook.models import MemoryFailure
from evernook.service import ServiceClient
from evernook.service_transport import authenticate_client, receive_json, send_json


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="evernook-service-")
        self.addCleanup(self.temporary.cleanup)
        self.config, self.access = initialize(Path(self.temporary.name) / "data",
            client_id="synthetic", entry="test")
        self.processes: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.stop_all)
        self.start()

    def stop_all(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            if process.stderr:
                process.stderr.close()

    def start(self) -> subprocess.Popen[bytes]:
        endpoint = self.config.parent / "memory-service.json"
        previous = endpoint.read_bytes() if endpoint.exists() else None
        process = subprocess.Popen([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"service exited {process.returncode}")
            if endpoint.exists() and endpoint.read_bytes() != previous:
                self.client = ServiceClient(self.config)
                self.client.call(self.access, "status", {})
                return process
            time.sleep(0.05)
        self.fail("service readiness timeout")

    def event(self) -> dict[str, object]:
        return asdict(Event(Source("test", "s", "main", "1"), "user", 1,
            None, "complete", "合成：我喜欢在湿地观察白鹭。"))

    def test_two_clients_receipts_conflict_and_restart(self) -> None:
        event = self.event()
        receipt = self.client.call(self.access, "remember", {"event": event})
        second = ServiceClient(self.config)
        self.assertEqual(second.call(self.access, "remember", {"event": event}), receipt)
        stored = second.call(self.access, "read", {"message_id": receipt["message_id"]})
        self.assertEqual(stored["event"], event)
        found = second.call(self.access, "search", {"query": "白鹭", "mode": "exact"})
        self.assertEqual(found["hits"][0]["message_id"], receipt["message_id"])
        recalled = second.call(self.access, "recall", {"query": "白鹭"})
        self.assertEqual(recalled["hits"][0]["evidence"]["message_id"], receipt["message_id"])
        event["text"] = "合成不同正文"
        with self.assertRaisesRegex(MemoryFailure, "source_conflict"):
            second.call(self.access, "remember", {"event": event})
        self.processes[0].terminate()
        self.processes[0].wait(timeout=15)
        with self.assertRaises(MemoryFailure):
            self.client.call(self.access, "status", {})
        self.start()
        self.assertEqual(self.client.call(self.access, "read",
            {"message_id": receipt["message_id"]})["event"]["text"], stored["event"]["text"])

    def test_wrong_identity_and_unknown_operation_do_not_echo_input(self) -> None:
        with self.assertRaises(MemoryFailure) as failure:
            self.client.call(replace(self.access, token="SYNTHETIC_TOKEN"), "remember",
                {"event": self.event()})
        self.assertNotIn("SYNTHETIC", str(failure.exception))
        with self.assertRaisesRegex(MemoryFailure, "unknown_service_operation"):
            self.client.call(self.access, "arbitrary-command", {"secret": "synthetic"})
        self.assertEqual(self.client.call(self.access, "status", {})["ledger"]["messages"], 0)

    def test_second_process_refuses_same_ledger(self) -> None:
        other = subprocess.run([sys.executable, "-B", "-m", "evernook.service",
            "--config", str(self.config)], capture_output=True, timeout=15)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn(b"service_already_running", other.stderr)
        self.client.call(self.access, "status", {})

    def test_wire_rejects_wrong_server_key_and_malformed_payload(self) -> None:
        endpoint = json.loads((self.config.parent / "memory-service.json").read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5) as connection:
            with self.assertRaises(MemoryFailure):
                authenticate_client(connection, b"wrong-key" * 4, time.monotonic() + 2)
        with socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5) as connection:
            deadline = time.monotonic() + 5
            authenticate_client(connection, bytes.fromhex(endpoint["key"]), deadline)
            send_json(connection, {"unexpected": "SYNTHETIC_SECRET"}, deadline)
            error = receive_json(connection, deadline)
            self.assertEqual(error["error"]["code"], "invalid_service_request")
            self.assertNotIn("SYNTHETIC", json.dumps(error))

    def test_json_frame_bound_and_duplicate_keys(self) -> None:
        from evernook.service_transport import MAX_FRAME
        for body in (b'{"a":1,"a":2}', b"not-json", b'{"a":NaN}'):
            left, right = socket.socketpair()
            with left, right:
                left.sendall(len(body).to_bytes(4, "big") + body)
                with self.assertRaisesRegex(MemoryFailure, "invalid_service_json"):
                    receive_json(right, time.monotonic() + 1)
        left, right = socket.socketpair()
        with left, right:
            left.sendall((MAX_FRAME + 1).to_bytes(4, "big"))
            with self.assertRaisesRegex(MemoryFailure, "service_frame_too_large"):
                receive_json(right, time.monotonic() + 1)
