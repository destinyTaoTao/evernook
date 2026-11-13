from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any
import zipfile

from probes.admission.local_reflection import collect, generation_options, model_template, verify_assets
from probes.admission.semantic_reflection import load_cases
from probes.admission.evidence_order import collect as collect_evidence_first


class LocalReflectionProbeTests(unittest.TestCase):
    def test_evidence_first_driver_preserves_fixed_conditions_and_tracks_itself(self) -> None:
        cases, _ = load_cases(Path('eval/zh-reflection-semantics-v1'))
        from probes.admission.semantic_reflection import packet
        from probes.admission import local_reflection
        payload, _ = packet(cases[0])
        def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.assertEqual(args[1], 'qwen35-2b-q4')
            self.assertEqual(kwargs, {'mode': 'greedy', 'seed': 12009, 'wire': 'flat', 'constrained': True, 'compact_memory': False})
            properties = getattr(local_reflection, 'schema')(payload, 'flat')['oneOf'][0]['properties']
            self.assertEqual(list(properties), ['support', 'action', 'value'])
            return {'identity': {}, 'collector_unchanged': True, 'scope': 'Synthetic-only.', 'quality_screen': False}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('probes.admission.evidence_order.native.collect', side_effect=run):
                result = collect_evidence_first(root, root, root / 'node', root / 'jinja')
            self.assertIn('evidence_order.py', result['identity'])
            self.assertTrue(result['collector_unchanged'])
            self.assertFalse(result['quality_screen'])
            self.assertEqual(json.loads((root / 'report.json').read_text(encoding='utf-8')), result)
        self.assertEqual(list(getattr(local_reflection, 'schema')(payload, 'flat')['oneOf'][0]['properties']), ['action', 'value', 'support'])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native diagnostic runner')
    def test_reference_binding_cannot_hide_an_unsupported_affirmation(self) -> None:
        cases, digest = load_cases(Path('eval/zh-reflection-semantics-v1'))
        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if command[1].endswith('native_chat.mjs'):
                return subprocess.CompletedProcess(command, 0, b'synthetic prefix\n', b'')
            size = len(Path(command[command.index('--binary-file') + 1]).read_bytes())
            header = ('Read ' + str(size) + ' bytes from binary file synthetic\n').encode()
            return subprocess.CompletedProcess(command, 0, b'{"action":"answer","value":"unsupported","support":["demo"]} [end of text]\n',
                header + b'0.00.010.001 I llama_completion: number of tokens in prompt = 20\n0.00.010.002 D n_remain: 511\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('probes.admission.local_reflection.verify_assets', return_value=({'file': 'synthetic.gguf'}, root, {})), \
                    patch('probes.admission.local_reflection.digest', return_value='synthetic'), \
                    patch('probes.admission.local_reflection.model_template', return_value='synthetic'), \
                    patch('probes.admission.local_reflection.load_cases', return_value=(cases[3:4], digest)), \
                    patch('probes.admission.local_reflection.subprocess.run', side_effect=run):
                result = collect(root, 'synthetic', root / 'output', root / 'node.exe', root / 'jinja',
                    wire='refs', constrained=True, compact_memory=True)
            self.assertEqual(result['unsupported_answers'], 1)
            self.assertFalse(result['quality_screen'])
            row = result['rows'][0]
            self.assertEqual(row['raw_judgment']['reason'], 'reflection_invalid_support')
            self.assertIn('demo', row['final_text'])
            self.assertNotIn('projected_text', row)
            self.assertIn('--json-schema-file', row['command'])
            self.assertEqual(row['command'][row['command'].index('-ub') + 1], '32')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native diagnostic runner')
    def test_native_runner_preserves_the_complete_generation_prefix(self) -> None:
        cases, digest = load_cases(Path('eval/zh-reflection-semantics-v1'))
        commands: list[list[str]] = []
        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            if command[1].endswith('native_chat.mjs'):
                return subprocess.CompletedProcess(command, 0, b'synthetic native prefix\n', b'')
            header = b''
            if '--binary-file' in command:
                size = len(Path(command[command.index('--binary-file') + 1]).read_bytes())
                header = ('Read ' + str(size) + ' bytes from binary file synthetic\n').encode()
            return subprocess.CompletedProcess(command, 0, b'{"action":"abstain","support":{}} [end of text]\n', header +
                b'0.00.010.001 I llama_completion: number of tokens in prompt = 20\n0.00.010.002 D n_remain: 511\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('probes.admission.local_reflection.verify_assets', return_value=({'file': 'synthetic.gguf'}, root, {})), \
                    patch('probes.admission.local_reflection.digest', return_value='synthetic'), \
                    patch('probes.admission.local_reflection.model_template', return_value='synthetic'), \
                    patch('probes.admission.local_reflection.load_cases', return_value=(cases[:1], digest)), \
                    patch('probes.admission.local_reflection.subprocess.run', side_effect=run):
                collect(root, 'synthetic', root / 'output', root / 'node.exe', root / 'jinja')
                failed_thinking = collect(root, 'synthetic', root / 'thinking-output', root / 'node.exe', root / 'jinja', mode='thinking')
            native = commands[-1]
            self.assertIn('--binary-file', native)
            self.assertNotIn('-f', native)
            self.assertEqual(Path(native[native.index('--binary-file') + 1]).read_bytes(), b'synthetic native prefix\n')
            row = failed_thinking['rows'][0]
            self.assertEqual(row['raw_judgment']['reason'], 'native_incomplete_thinking')
            self.assertEqual((row['input_tokens'], row['output_tokens']), (20, 1))
            self.assertEqual(row['finish'], 'eog')

    def test_profile_decoding_does_not_reuse_another_models_parameters(self) -> None:
        spec = {'generation': {'thinking': True, 'temperature': 0.05, 'top_k': 50, 'repeat_penalty': 1.05}}
        thinking, options = generation_options(spec, 'profile', 120102)
        self.assertTrue(thinking)
        self.assertEqual(options[options.index('--temp') + 1], '0.05')
        self.assertEqual(options[options.index('--top-k') + 1], '50')
        self.assertEqual(options[options.index('--repeat-penalty') + 1], '1.05')
        self.assertFalse(generation_options({}, 'greedy', 12009)[0])
        for invalid in ({}, {'generation': {'thinking': 'yes'}}):
            with self.subTest(spec=invalid), self.assertRaises(ValueError):
                generation_options(invalid, 'profile', 120102)

    def test_profile_nucleus_sampling_uses_the_authors_probability(self) -> None:
        spec = {'generation': {'thinking': False, 'temperature': 1.0, 'top_k': 0, 'repeat_penalty': 1.0, 'top_p': 0.95}}
        thinking, options = generation_options(spec, 'profile', 120121)
        self.assertFalse(thinking)
        self.assertEqual(options[options.index('--top-p') + 1], '0.95')
        self.assertEqual(options[options.index('--top-k') + 1], '0')
        for probability in (0, 1.01, float('nan'), True, '0.95'):
            invalid = {'generation': {**spec['generation'], 'top_p': probability}}
            with self.subTest(top_p=probability), self.assertRaisesRegex(ValueError, 'invalid_candidate_generation'):
                generation_options(invalid, 'profile', 120121)
        old = {'generation': {'thinking': False, 'temperature': 0.1, 'top_k': 50, 'repeat_penalty': 1.05}}
        _, old_options = generation_options(old, 'profile', 120121)
        self.assertEqual(old_options[old_options.index('--top-p') + 1], '1')

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
