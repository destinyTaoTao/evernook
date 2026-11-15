from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from evernook import Event, Source, initialize, pair_client
from evernook.service import ServiceClient


class OptionalVaultServiceTests(unittest.TestCase):
    def test_plain_activated_service_does_not_import_optional_vault_crypto(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-no-vault-service-') as temporary:
            config, access = initialize(Path(temporary) / 'runtime', client_id='synthetic', entry='fixture')
            owner = config.parent / 'mcp-access-owner.json'
            pair_client(config, client_id=access.client_id, entry='fixture', access_path=owner)
            code = '''import builtins,pathlib,sys
original=builtins.__import__
def without_vault(name,*args,**kwargs):
    if name=='nacl' or name.startswith('nacl.'): raise ModuleNotFoundError('synthetic optional dependency absent')
    return original(name,*args,**kwargs)
builtins.__import__=without_vault
from evernook.service import serve
serve(pathlib.Path(sys.argv[1]),activate_owner=pathlib.Path(sys.argv[2]))
'''
            child = subprocess.Popen([sys.executable, '-B', '-c', code, str(config), str(owner)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 10
                while not (config.parent / 'memory-service.json').exists():
                    if child.poll() is not None:
                        assert child.stderr is not None
                        self.fail(child.stderr.read().decode('utf8', errors='replace'))
                    if time.monotonic() > deadline:
                        self.fail('plain service startup timed out')
                    time.sleep(.02)
                client = ServiceClient(config)
                event = Event(Source('fixture', 'synthetic', 'main', 'one'), 'user', 1, None, 'complete', '合成普通记忆')
                receipt = client.call(access, 'remember', {'event': asdict(event)})
                self.assertTrue(receipt['original_saved'])
                self.assertEqual(client.call(access, 'read', {'message_id': receipt['message_id']})['event'], asdict(event))
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
                if child.stderr:
                    child.stderr.close()
