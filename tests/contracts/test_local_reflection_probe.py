from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from probes.admission.local_reflection import model_template, verify_assets


class LocalReflectionProbeTests(unittest.TestCase):
    def test_native_template_is_preserved_and_incomplete_gguf_is_rejected(self) -> None:
        def text(value: str) -> bytes:
            data = value.encode(); return struct.pack('<Q', len(data)) + data
        native = '{{ messages[0].content }}'
        data = (b'GGUF' + struct.pack('<IQQ', 3, 0, 2)
            + text('synthetic.array') + struct.pack('<IIQII', 9, 4, 2, 1, 2)
            + text('tokenizer.chat_template') + struct.pack('<I', 8) + text(native))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic.gguf'; path.write_bytes(data)
            self.assertEqual(model_template(path), native)
            for invalid in (b'bad', data[:-1], data.replace(b'GGUF', b'NOPE', 1)):
                path.write_bytes(invalid)
                with self.subTest(value=invalid[:8]), self.assertRaises(ValueError):
                    model_template(path)

    def test_native_binary_and_model_tampering_fail_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime = root / 'runtime'; runtime.mkdir()
            jinja = root / 'jinja/dist'; jinja.mkdir(parents=True)
            (jinja / 'index.js').write_bytes(b'synthetic jinja; never executed')
            (root / 'model.gguf').write_bytes(b'synthetic model; never executed')
            archive = root / 'runtime.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('llama-completion.exe', b'synthetic binary; never executed')
            with zipfile.ZipFile(archive) as source:
                source.extractall(runtime)
            def sha(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'runtime': {'archive': archive.name, 'directory': runtime.name, 'sha256': sha(archive)},
                'jinja': {'sha256': sha(jinja / 'index.js')}, 'models': [{'id': 'synthetic', 'file': 'model.gguf',
                    'bytes': (root / 'model.gguf').stat().st_size, 'sha256': sha(root / 'model.gguf')}]}), encoding='utf-8')
            with patch('probes.admission.local_reflection.MANIFEST', manifest):
                self.assertEqual(verify_assets(root, 'synthetic', jinja.parent)[0]['id'], 'synthetic')
                (runtime / 'unexpected.dll').write_bytes(b'never executed')
                with self.assertRaisesRegex(ValueError, 'unexpected_native_binary'):
                    verify_assets(root, 'synthetic', jinja.parent)
                (runtime / 'unexpected.dll').unlink()
                (runtime / 'llama-completion.exe').write_bytes(b'changed binary')
                with self.assertRaisesRegex(ValueError, 'integrity_mismatch'):
                    verify_assets(root, 'synthetic', jinja.parent)
                with zipfile.ZipFile(archive) as source:
                    (runtime / 'llama-completion.exe').write_bytes(source.read('llama-completion.exe'))
                (root / 'model.gguf').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'integrity_mismatch'):
                    verify_assets(root, 'synthetic', jinja.parent)
