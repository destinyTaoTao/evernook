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
from typing import Any

from evernook import Access, Event, Source, initialize
from evernook.runtime import pair_client
from evernook.models import MemoryFailure
from evernook.service import ServiceClient, client_access
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
            "--config", str(self.config), "--allow-legacy-shared"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                diagnostic = process.stderr.read().decode('utf-8', errors='replace') if process.stderr else ''
                self.fail(f"service exited {process.returncode}: {diagnostic}")
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
        self.assertFalse(self.client.call(self.access, 'status', {})['service']['single_writer'])
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

    @unittest.skipUnless(sys.platform == 'win32', 'Windows ACL regression')
    def test_real_acl_change_between_access_and_endpoint_checks_is_rejected(self) -> None:
        access_path = self.config.parent / 'mcp-access-synthetic.json'
        pair_client(self.config, client_id=self.access.client_id, entry='test', access_path=access_path)
        endpoint = self.config.parent / 'memory-service.json'
        everyone = '*S-1-1-0'

        def acl(path: Path, *arguments: str) -> None:
            # This test changes only files under its freshly created temporary runtime.
            self.assertTrue(path.resolve().is_relative_to(Path(self.temporary.name).resolve()))
            subprocess.run(['icacls.exe', str(path), *arguments], capture_output=True, check=True, timeout=10)

        loaded = client_access(self.config, access_path)
        try:
            acl(endpoint, '/grant', everyone + ':R')
            with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                self.client.call(loaded, 'status', {})
        finally:
            acl(endpoint, '/remove:g', everyone)
        self.assertEqual(self.client.call(loaded, 'status', {})['ledger']['messages'], 0)
        try:
            acl(access_path, '/grant', everyone + ':R')
            with self.assertRaisesRegex(MemoryFailure, 'unsafe_runtime_permissions'):
                client_access(self.config, access_path)
        finally:
            acl(access_path, '/remove:g', everyone)
        self.assertEqual(client_access(self.config, access_path), loaded)

    def test_versioned_management_projection_across_paired_clients_and_restart(self) -> None:
        def manage(action: str) -> dict[str, Any]:
            return self.client.call(self.access, 'cli', {'command': 'manage',
                'input': {'action': action, 'view': 'timeline'}})
        initial = manage('read-versioned')
        self.assertEqual(manage('fingerprint'), {'token': initial['token']})
        self.assertEqual(manage('fingerprint'), {'token': initial['token']})
        paired = self.client.call(self.access, 'pair-client', {'client_id': 'peer', 'entry': 'peer',
            'scopes': ['capture', 'recall']})
        peer = Access(**json.loads(Path(paired['access_file']).read_text(encoding='utf-8')))
        second = ServiceClient(self.config)
        event = asdict(Event(Source('peer', 'synthetic', 'main', '1'), 'user', 1,
                             None, 'complete', '合成：跨客户端记录'))
        receipt = second.call(peer, 'remember', {'event': event})
        changed = manage('fingerprint')
        self.assertNotEqual(initial['token'], changed['token'])
        self.assertNotIn('合成', json.dumps(changed, ensure_ascii=False))
        requests = [{'view': 'timeline'}, {'view': 'source', 'message_id': receipt['message_id']}]
        batch = self.client.call(self.access, 'cli', {'command': 'manage', 'input': {'action': 'fingerprints', 'requests': requests}})
        self.assertEqual(len(batch['tokens']), 2)
        self.assertEqual(batch['tokens'][0], changed['token'])
        self.assertNotIn('合成', json.dumps(batch, ensure_ascii=False))
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            second.call(peer, 'cli', {'command': 'manage', 'input': {'action': 'fingerprint', 'view': 'timeline'}})
        with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
            second.call(peer, 'cli', {'command': 'manage', 'input': {'action': 'fingerprints', 'requests': requests}})
        self.processes[0].terminate()
        self.processes[0].wait(timeout=15)
        self.start()
        self.assertNotEqual(manage('fingerprint')['token'], changed['token'])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows host-owned worker')
    def test_fingerprint_worker_only_returns_bounded_authorized_tokens(self) -> None:
        access_path = self.config.parent / 'mcp-access-synthetic.json'
        pair_client(self.config, client_id=self.access.client_id, entry='test', access_path=access_path)
        request = {'id': 'a' * 32, 'requests': [{'view': 'timeline'}]}
        invalid = {'id': 'b' * 32, 'requests': [{'view': 'timeline', 'secret': 'synthetic-never-echo'}]}
        worker = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.fingerprint_worker', '--config', str(self.config),
            '--access', str(access_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert worker.stdin and worker.stdout
            def ask(value: dict[str, Any]) -> dict[str, Any]:
                assert worker.stdin and worker.stdout
                worker.stdin.write(json.dumps(value) + '\n'); worker.stdin.flush()
                return json.loads(worker.stdout.readline())
            first = ask(request)
            self.assertRegex(first['tokens'][0], r'^[a-f0-9]{64}$')
            self.client.call(self.access, 'remember', {'event': self.event()})
            self.assertNotEqual(first['tokens'], ask({**request, 'id': 'c' * 32})['tokens'])
            self.assertEqual(ask(invalid), {'id': 'b' * 32, 'error': 'fingerprint_unavailable'})
            paired = self.client.call(self.access, 'pair-client', {'client_id': 'capture-only', 'entry': 'other',
                'scopes': ['capture', 'recall']})
            original_access = access_path.read_bytes()
            access_path.write_bytes(Path(paired['access_file']).read_bytes())
            self.assertEqual(ask({**request, 'id': 'e' * 32}), {'id': 'e' * 32, 'error': 'fingerprint_unavailable'})
            access_path.write_bytes(original_access)
            self.processes[0].terminate(); self.processes[0].wait(timeout=15)
            self.assertEqual(ask({**request, 'id': 'd' * 32}), {'id': 'd' * 32, 'error': 'fingerprint_unavailable'})
        finally:
            worker.kill()
            out, err = worker.communicate(timeout=15)
            self.assertNotIn('synthetic-never-echo', out + err)

    def test_trusted_learning_commands_use_service_and_deny_capture_only_clients(self) -> None:
        def command(name: str, value: dict[str, object]) -> object:
            return self.client.call(self.access, 'cli', {'command': name, 'input': value})
        status = self.client.call(self.access, 'cli', {'command': 'preference-update-status', 'input': {}})
        command('preference-update-configure', {'enabled': True, 'admitted_processor': status['settings']['rules_processor']})
        first = self.client.call(self.access, 'remember', {'event': asdict(Event(Source('test', 'a', 'main', '1'),
            'user', 1, None, 'complete', '今天聊了很久。说到日常交流，我偏好分点回答。'))})['message_id']
        report = self.client.call(self.access, 'cli', {'command': 'preference-update-process', 'input': {'source_ids': [first]}})
        self.assertEqual(report['processing']['merged_or_current_tags'], 1)
        second = self.client.call(self.access, 'remember', {'event': asdict(Event(Source('test', 'b', 'main', '1'),
            'assistant', 1, None, 'complete', '合成：我们聊过分点表达。'))})['message_id']
        episode = self.client.call(self.access, 'cli', {'command': 'episode-propose', 'input': {'source_ids': [first, second]}})
        self.assertEqual(episode['state'], 'pending_review')
        self.assertEqual(command('episode-configure', {'enabled': True}), {'enabled': True, 'publication': 'review_only'})
        ordinary = pair_client(self.config, client_id='ordinary', entry='ordinary',
            access_path=self.config.parent / 'mcp-access-ordinary.json', scopes=('capture', 'recall'))
        for name in ('episode-configure', 'preference-update-configure', 'observation-configure'):
            with self.subTest(command=name), self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                self.client.call(ordinary, 'cli', {'command': name, 'input': {'enabled': True}})

    def test_topic_edit_and_observation_split_return_nested_receipts_over_real_transport(self) -> None:
        def command(name: str, value: dict[str, object]) -> Any:
            return self.client.call(self.access, 'cli', {'command': name, 'input': value})
        command('topic-view-define', {'request_id': 'define', 'view_id': 'wetland', 'expected_revision': 0,
            'definition': {'title': '湿地', 'purpose': '合成专题', 'query': '湿地'}})
        edited = command('topic-view-edit', {'request_id': 'edit', 'view_id': 'wetland', 'expected_revision': 1, 'text': '合成：湿地见白鹭。'})
        self.assertEqual(edited['view']['definition_revision'], 2)
        self.assertTrue(edited['source_receipt']['original_saved'])
        repeated = command('topic-view-edit', {'request_id': 'edit', 'view_id': 'wetland', 'expected_revision': 1, 'text': '合成：湿地见白鹭。'})
        self.assertEqual(repeated['source_receipt'], edited['source_receipt'])
        second_text = '合成：桥边见苍鹭。'
        second = self.client.call(self.access, 'remember', {'event': asdict(Event(Source('test', 'second', 'main', '1'),
            'user', 1, None, 'complete', second_text))})
        spans = [{'message_id': receipt['message_id'], 'state_version': 1, 'representation': 'original', 'content_version': 1,
            'start': 0, 'end': len(text), 'expected_text': text} for receipt, text in
            ((edited['source_receipt'], '合成：湿地见白鹭。'), (second, second_text))]
        draft: dict[str, object] = {'topic_key': 'user:birds', 'conclusion': '合成：两个不同来源的鸟类经历。', 'support': spans,
            'counterevidence': [], 'category': 'experience', 'valid_from': None, 'valid_until': None,
            'processor_version': 'synthetic', 'policy_version': 'review-only'}
        parent = command('observation-propose', {'request_id': 'parent', 'draft': draft})
        result = command('observation-split', {'request_id': 'split', 'observation_id': parent['observation_id'],
            'expected_revision': 1, 'confirmation_id': second['message_id'],
            'drafts': [{**draft, 'topic_key': 'user:one', 'support': spans[:1]}, {**draft, 'topic_key': 'user:two', 'support': spans[1:]}]})
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_graph_topic_node_invalidates_after_second_client_write(self) -> None:
        def command(name: str, value: dict[str, object]) -> Any:
            return self.client.call(self.access, 'cli', {'command': name, 'input': value})
        self.client.call(self.access, 'remember', {'event': self.event()})
        command('topic-view-define', {'request_id': 'define', 'view_id': 'wetland', 'expected_revision': 0,
            'definition': {'title': '湿地', 'purpose': '合成专题', 'query': '湿地'}})
        self.assertEqual(command('topic-view-refresh', {'view_id': 'wetland', 'timeout': 10})['state'], 'valid')
        page = command('manage', {'action': 'read-versioned', 'view': 'network_graph'})
        self.assertTrue(any(node['kind'] == 'topic' for node in page['value']['nodes']))
        paired = self.client.call(self.access, 'pair-client', {'client_id': 'peer-topic', 'entry': 'peer',
            'scopes': ['capture', 'recall']})
        peer = Access(**json.loads(Path(paired['access_file']).read_text(encoding='utf-8')))
        event = Event(Source('peer', 'synthetic', 'main', 'new'), 'user', 1, None, 'complete', '合成：另一个入口新消息。')
        ServiceClient(self.config).call(peer, 'remember', {'event': asdict(event)})
        token = command('manage', {'action': 'fingerprint', 'view': 'network_graph'})
        self.assertNotEqual(page['token'], token['token'])
        changed = command('manage', {'action': 'read', 'view': 'network_graph'})
        self.assertFalse(any(node['kind'] == 'topic' for node in changed['nodes']))

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
