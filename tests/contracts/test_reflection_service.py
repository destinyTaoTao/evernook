from __future__ import annotations

from dataclasses import asdict
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import initialize, pair_client
from evernook.models import MemoryFailure
from evernook.service import ServiceClient


class ReflectionServiceTests(unittest.TestCase):
    def test_service_owned_configuration_read_only_mcp_bridge_and_secret_routing(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-reflection-service-') as temporary:
            root = Path(temporary)
            config, access = initialize(root / 'runtime', client_id='owner', entry='owner')
            owner_path = config.parent / 'mcp-access-owner.json'
            owner_path.write_text(json.dumps(asdict(access)), encoding='utf-8')
            reader_path = config.parent / 'mcp-access-reader.json'
            reader = pair_client(config, client_id='reader', entry='reader', access_path=reader_path, scopes=('recall',))
            profile = root / 'reflection.json'
            # These paths are never executed: the private query must stop before model access.
            profile.write_text(json.dumps({'schema_version': 1, 'profile': 'qwen3-0.6b-experimental-reflection-v1',
                'node': sys.executable, 'transformers_dir': str(root), 'model_dir': str(root), 'policy': {}}), encoding='utf-8')
            process = subprocess.Popen([sys.executable, '-B', '-m', 'evernook.service', '--config', str(config),
                '--reflection-config', str(profile)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                end = time.monotonic() + 15
                while not (config.parent / 'memory-service.json').exists():
                    self.assertIsNone(process.poll())
                    if time.monotonic() >= end:
                        self.fail('service startup timeout')
                    time.sleep(.05)
                client = ServiceClient(config)
                client.call(access, 'cli', {'command': 'reflect-configure', 'input': {}})
                with self.assertRaisesRegex(MemoryFailure, 'permission_denied'):
                    client.call(reader, 'cli', {'command': 'reflect-configure', 'input': {}})
                listing = client.call(reader, 'mcp-list', {'profile': 'recall'})
                self.assertIn('evernook_v1_reflect', {item['name'] for item in listing['tools']})
                self.assertFalse(any('configure' in item['name'] for item in listing['tools']))
                value = client.call(reader, 'mcp', {'profile': 'recall', 'name': 'evernook_v1_reflect',
                    'arguments': {'query': '我的密码是Synthetic-Only-39!', 'timeout': 10}})
                self.assertEqual(value['state'], 'protected_route_required')
                self.assertEqual(value['evidence'], [])
                self.assertNotIn('Synthetic-Only', json.dumps(value))
                async def bridge() -> None:
                    from mcp import Client, StdioServerParameters
                    parameters = StdioServerParameters(command=sys.executable, args=['-B', '-m', 'evernook.mcp_server',
                        '--config', str(config), '--access', str(reader_path), '--require-service'])
                    async with Client(parameters, read_timeout_seconds=15) as mcp:
                        result = await mcp.call_tool('evernook_v1_reflect', {'query': '我的密码是Synthetic-Only-39!', 'timeout': 10})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content['result']['state'], 'protected_route_required')
                asyncio.run(bridge())
            finally:
                process.terminate(); process.wait(15)
                if process.stderr:
                    process.stderr.close()


if __name__ == '__main__':
    unittest.main()
