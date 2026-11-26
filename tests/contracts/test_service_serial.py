from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from evernook import Event, MemoryFailure, MemoryRuntime, Source, initialize
from evernook.service import ENDPOINT, ServiceClient, _Handler, _Server, dispatch
from evernook.service_transport import OPEN_SERIAL, SERIAL_READY, authenticate_client, authenticate_server, receive_json, send_json


class SerialServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="evernook-serial-test-")
        self.addCleanup(directory.cleanup)
        self.config, self.access = initialize(Path(directory.name) / "ledger", client_id="synthetic", entry="test")
        self.runtime = MemoryRuntime(self.config)
        self.server = _Server(self.runtime, os.urandom(32))
        self.addCleanup(self.server.server_close)
        endpoint = {"version": 1, "port": self.server.server_address[1], "key": self.server.key.hex()}
        (self.config.parent / ENDPOINT).write_text(json.dumps(endpoint), encoding="utf-8")
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.client = self.serial_client()

    def serial_client(self) -> ServiceClient:
        client = ServiceClient(self.config, reuse=True)
        self.addCleanup(client.close)
        return client

    def test_same_socket_rechecks_identity_even_with_same_file_size_and_timestamp(self) -> None:
        with patch("evernook.service.authenticate_client", wraps=authenticate_client) as auth:
            self.client.call(self.access, "status", {})
            credentials = self.config.parent / "client.json"
            info = credentials.stat()
            content = credentials.read_bytes()
            token = self.access.token.encode()
            changed = (b"b" if token[:1] == b"a" else b"a") + token[1:]
            self.assertIn(token, content)
            credentials.write_bytes(content.replace(token, changed))
            os.utime(credentials, ns=(info.st_atime_ns, info.st_mtime_ns))
            with self.assertRaisesRegex(MemoryFailure, "unauthorized"):
                self.client.call(self.access, "status", {})
            self.assertEqual(auth.call_count, 1)
            self.client.close()
            with self.assertRaisesRegex(MemoryFailure, "service_client_closed"):
                self.client.call(self.access, "status", {})

    def test_ambiguous_write_is_never_replayed(self) -> None:
        self.client.call(self.access, "status", {})
        event = Event(Source("test", "session", "main", "1"), "user", 1, None, "complete", "synthetic")

        def lost_response(connection: socket.socket, deadline: float, cancel: threading.Event | None = None) -> dict[str, Any]:
            frame = receive_json(connection, deadline, cancel)
            if connection.getpeername()[1] == self.server.server_address[1]:
                raise MemoryFailure("service_disconnected")
            return frame

        # The socket was warmed already, so this receive is the business reply.
        with patch("evernook.service.receive_json", side_effect=lost_response), \
                patch("evernook.service.dispatch", wraps=dispatch) as calls:
            with self.assertRaisesRegex(MemoryFailure, "service_disconnected"):
                self.client.call(self.access, "remember", {"event": asdict(event)})
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(self.client.call(self.access, "status", {})["ledger"]["messages"], 1)

    def test_endpoint_key_rotation_discards_old_authenticated_connection(self) -> None:
        self.client.call(self.access, "status", {})
        path = self.config.parent / ENDPOINT
        endpoint = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**endpoint, "key": os.urandom(32).hex()}), encoding="utf-8")
        with patch("evernook.service.dispatch", wraps=dispatch) as requests:
            with self.assertRaisesRegex(MemoryFailure, "service_authentication_failed"):
                self.client.call(self.access, "status", {})
            self.assertFalse(requests.called)
        path.write_text(json.dumps(endpoint), encoding="utf-8")
        self.assertIn("ledger", self.client.call(self.access, "status", {}))

    def test_idle_sessions_leave_all_eight_operation_slots_available(self) -> None:
        for _ in range(4):
            self.serial_client().call(self.access, "status", {})
        eight = threading.Barrier(8, timeout=8)

        def simultaneous(*args: object, **kwargs: object) -> object:
            eight.wait()
            return {}

        with patch("evernook.service.dispatch", side_effect=simultaneous), ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(ServiceClient(self.config).call, self.access, "status", {}) for _ in range(8)]
            for job in jobs:
                self.assertIn("service", job.result(12))

    def test_busy_and_cancellation_on_warm_socket_do_not_resend(self) -> None:
        self.client.call(self.access, "status", {})
        with patch("evernook.service.dispatch", wraps=dispatch) as requests:
            for _ in range(8):
                self.assertTrue(self.server.slots.acquire(timeout=1))
            try:
                with self.assertRaisesRegex(MemoryFailure, "service_busy"):
                    self.client.call(self.access, "status", {})
                self.assertEqual(requests.call_count, 0)
            finally:
                for _ in range(8):
                    self.server.slots.release()
        self.client.call(self.access, "status", {})
        entered, stopped, cancel = threading.Event(), threading.Event(), threading.Event()

        def cooperative(*args: object, **kwargs: Any) -> object:
            entered.set()
            if kwargs["cancel"].wait(3):
                stopped.set()
            return {}

        with patch("evernook.service.dispatch", side_effect=cooperative) as requests, ThreadPoolExecutor(1) as pool:
            job = pool.submit(self.client.call, self.access, "status", {}, cancel=cancel)
            self.assertTrue(entered.wait(2))
            cancel.set()
            with self.assertRaisesRegex(MemoryFailure, "service_canceled"):
                job.result(2)
            self.assertTrue(stopped.wait(2))
            self.assertEqual(requests.call_count, 1)

    def test_serial_session_refuses_admin_and_protected_frames(self) -> None:
        for name in ("pair-client", "protected-receive", "cli"):
            with self.subTest(name=name), socket.create_connection(("127.0.0.1", self.server.server_address[1])) as sock:
                deadline = time.monotonic() + 3
                authenticate_client(sock, self.server.key, deadline)
                send_json(sock, OPEN_SERIAL, deadline)
                self.assertEqual(receive_json(sock, deadline), SERIAL_READY)
                send_json(sock, {"version": 1, "operation": name, "access": asdict(self.access),
                                 "arguments": {}}, deadline)
                self.assertEqual(receive_json(sock, deadline)["error"]["code"], "operation_not_allowed")

    def test_legacy_server_sees_one_negotiation_and_each_business_request_once(self) -> None:
        frames: list[str] = []

        def legacy(handler: _Handler) -> None:
            deadline = time.monotonic() + 5
            authenticate_server(handler.request, self.server.key, deadline)
            request = receive_json(handler.request, deadline)
            frames.append(str(request.get("operation", "negotiation")))
            try:
                value = dispatch(self.runtime, request)
                reply = {"version": 1, "ok": True, "result": value}
            except MemoryFailure as error:
                reply = {"version": 1, "ok": False, "error": {"code": error.code}}
            send_json(handler.request, reply, deadline)

        with patch.object(_Handler, "handle", legacy):
            for _ in range(2):
                self.assertIn("ledger", self.client.call(self.access, "status", {}))
        self.assertEqual(frames, ["negotiation", "status", "status"])
