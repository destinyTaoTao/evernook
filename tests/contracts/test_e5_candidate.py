from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from evernook.models import MemoryFailure
from probes.admission import e5_candidate


class E5CandidateTests(unittest.TestCase):
    def test_roles_asset_validation_and_no_network_response(self) -> None:
        with tempfile.TemporaryDirectory(prefix='evernook-e5-probe-') as temporary:
            root = Path(temporary)
            (root / 'asset').write_bytes(b'synthetic asset')
            backend = e5_candidate.Candidate(root, root / 'deps', root / 'node')
            files = {'asset': hashlib.sha256(b'synthetic asset').hexdigest()}
            response = subprocess.CompletedProcess([], 0, stdout=b'{"vectors":[[1,0]],"network_attempts":0}')
            with patch.dict(e5_candidate.FILES, files, clear=True), patch.object(e5_candidate.subprocess, 'run', return_value=response) as run:
                for role in ('query', 'passage'):
                    backend.role = role
                    self.assertEqual(backend.embed(('合成文本',), timeout=5), ((1, 0),))
                    payload = json.loads(run.call_args.kwargs['input'])
                    self.assertEqual(payload['texts'], [role + ': 合成文本'])
                backend.role = None
                with self.assertRaisesRegex(MemoryFailure, 'embedding_role_missing'):
                    backend.embed(('合成文本',), timeout=5)
                canceled = threading.Event()
                canceled.set()
                with self.assertRaisesRegex(MemoryFailure, 'embedding_canceled'):
                    backend.embed(('合成文本',), timeout=5, cancel=canceled)
                backend.role = 'query'
                run.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"vectors":[[1,0]],"network_attempts":1}')
                with self.assertRaisesRegex(MemoryFailure, 'embedding_unavailable'):
                    backend.embed(('合成文本',), timeout=5)
                (root / 'asset').write_bytes(b'changed')
                with self.assertRaisesRegex(MemoryFailure, 'embedding_integrity_mismatch'):
                    backend.embed(('合成文本',), timeout=5)
                self.assertEqual(run.call_count, 3)


if __name__ == '__main__':
    unittest.main()
