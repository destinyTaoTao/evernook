from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class OldCoreStateTests(unittest.TestCase):
    def test_old_core_rejects_recovered_multi_config(self) -> None:
        python = os.environ.get('EVERNOOK_TEST_OLD_STATE_PYTHON')
        if not python:
            self.skipTest('old installed Core not configured')
        with tempfile.TemporaryDirectory(prefix='evernook-old-state-') as temporary:
            root = Path(temporary)
            config = root / 'runtime.json'
            config.write_text(json.dumps({'schema_version': 1, 'data_dir': str(root), 'busy_timeout_ms': 5000,
                                          'service_mode': True, 'restored_missing_vault': True,
                                          'protected_multi_binding': True}), encoding='utf-8')
            code = 'from evernook import MemoryRuntime; from pathlib import Path; import sys; MemoryRuntime(Path(sys.argv[1]), service_owner=True)'
            result = subprocess.run([python, '-B', '-c', code, str(config)], env={**os.environ, 'PYTHONPATH': ''},
                                    cwd=temporary, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'unsupported_config', result.stderr)


if __name__ == '__main__':
    unittest.main()
