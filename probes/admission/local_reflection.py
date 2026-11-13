"""Windows-only, synthetic-only GGUF admission diagnostic; never a production backend."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from typing import Any
import zipfile

from evernook.paths import external
from evernook.reflection_protocol import build_messages
from probes.admission.native_completion import completion_text, token_usage
from probes.admission.semantic_reflection import judge_generated, load_cases, packet

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'local-reflection-candidates.json'


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def model_template(path: Path) -> str:
    """Read the native template from a previously hash-verified GGUF, not a new prompt."""
    with path.open('rb') as source:
        def exact(length: int) -> bytes:
            value = source.read(length)
            if len(value) != length:
                raise ValueError('invalid_gguf')
            return value
        def number(fmt: str) -> int:
            return int(struct.unpack('<' + fmt, exact(struct.calcsize('<' + fmt)))[0])
        def text() -> str:
            length = number('Q')
            if length > 65536:
                raise ValueError('invalid_gguf')
            return exact(length).decode('utf-8')
        def skip(kind: int, depth: int = 0) -> None:
            if depth > 2:
                raise ValueError('invalid_gguf')
            if kind == 8:
                source.seek(number('Q'), 1)
            elif kind == 9:
                element, count = number('I'), number('Q')
                if count > 1000000:
                    raise ValueError('invalid_gguf')
                for _ in range(count):
                    skip(element, depth + 1)
            else:
                size = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}.get(kind)
                if size is None:
                    raise ValueError('invalid_gguf')
                exact(size)
        if exact(4) != b'GGUF' or number('I') != 3:
            raise ValueError('invalid_gguf')
        number('Q')  # Tensor count; no tensor data is interpreted here.
        count = number('Q')
        if not 1 <= count <= 4096:
            raise ValueError('invalid_gguf')
        for _ in range(count):
            key, kind = text(), number('I')
            if key == 'tokenizer.chat_template':
                if kind != 8:
                    raise ValueError('invalid_gguf')
                template = text()
                if not template.strip():
                    raise ValueError('missing_native_template')
                return template
            skip(kind)
    raise ValueError('missing_native_template')


def verify_assets(assets: Path, profile: str, jinja: Path) -> tuple[dict[str, Any], Path, dict[str, str]]:
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    spec = next((row for row in manifest['models'] if row['id'] == profile), None)
    if spec is None:
        raise ValueError('unknown_candidate')
    runtime = manifest['runtime']
    archive, directory = assets / runtime['archive'], assets / runtime['directory']
    if digest(archive) != runtime['sha256'] or digest(jinja / 'dist/index.js') != manifest['jinja']['sha256']:
        raise ValueError('candidate_integrity_mismatch')
    with zipfile.ZipFile(archive) as source:
        members = [row for row in source.infolist() if not row.is_dir()]
        for member in members:
            path = (directory / member.filename).resolve()
            if not path.is_relative_to(directory.resolve()) or digest(path) != hashlib.sha256(source.read(member)).hexdigest():
                raise ValueError('candidate_integrity_mismatch')
        expected = {row.filename.casefold() for row in members if Path(row.filename).suffix.lower() in {'.dll', '.exe'}}
        actual = {row.name.casefold() for row in directory.iterdir() if row.suffix.lower() in {'.dll', '.exe'}}
        if actual != expected:
            raise ValueError('unexpected_native_binary')
    model = assets / spec['file']
    if model.stat().st_size != spec['bytes'] or digest(model) != spec['sha256']:
        raise ValueError('candidate_integrity_mismatch')
    paths = {'local-reflection-candidates.json': MANIFEST, 'local_reflection.py': Path(__file__).resolve(),
        **{name: HERE / name for name in ('native_chat.mjs', 'native_completion.py', 'semantic_reflection.py')}}
    identity = {name: digest(path) for name, path in paths.items()}
    return spec, directory, identity


def collect(assets: Path, profile: str, output: Path, node: Path, jinja: Path, *, mode: str = 'greedy', seed: int = 12009) -> dict[str, Any]:
    if sys.platform != 'win32' or mode not in {'greedy', 'sampling', 'thinking'}:
        raise ValueError('unsupported_native_diagnostic')
    assets, output, node, jinja = (external(path) for path in (assets, output, node, jinja))
    spec, runtime, identity = verify_assets(assets, profile, jinja)
    identity['node'] = digest(node)
    protocol = HERE.parents[1] / 'src/evernook/reflection_protocol.py'
    identity['protocol'] = digest(protocol)
    cases, cases_hash = load_cases(HERE.parents[1] / 'eval/zh-reflection-semantics-v1')
    template = model_template(assets / spec['file'])
    output.mkdir()
    rows = []
    # CLI policy is explicit, not inherited from an ambient HF/RPC/GPU configuration.
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('LLAMA_ARG_') and key.upper() != 'HF_TOKEN'}
    for case in cases:
        payload, aliases = packet(case)
        messages = build_messages(payload)
        root = output / case['id']; root.mkdir()
        (root / 'messages.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
        started = time.monotonic()
        row: dict[str, Any] = {'case_id': case['id'], 'expected_answerable': case['expected']['decision'] == 'answer'}
        stage = 'render'
        try:
            rendered = subprocess.run([str(node), str(HERE / 'native_chat.mjs')], input=json.dumps({'template': template,
                'messages': messages, 'jinja_dir': str(jinja), 'thinking': mode == 'thinking'}, ensure_ascii=False).encode(),
                capture_output=True, check=True, timeout=10, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            prompt = root / 'prompt.txt'; prompt.write_bytes(rendered.stdout)
            command = [str(runtime / 'llama-completion.exe'), '-m', str(assets / spec['file']), '--offline', '-no-cnv',
                '--no-escape', '--no-display-prompt', '--simple-io', '--no-warmup', '--perf', '--log-colors', 'off',
                '--log-verbosity', '5', '--verbose-prompt', '-f', str(prompt), '-n', '512', '-c', '4608', '-t', '4', '-tb', '4',
                '-b', '128', '-ub', '128', '-ngl', '0', '--temp', '0' if mode == 'greedy' else '1', '--seed', str(seed),
                '--no-repack', '--no-context-shift']
            if mode != 'greedy':
                command += ['--top-k', '20', '--top-p', '0.95' if mode == 'thinking' else '1', '--min-p', '0',
                    '--presence-penalty', '1.5' if mode == 'thinking' else '2', '--repeat-penalty', '1', '--repeat-last-n', '4608']
            row.update(command=command, prompt_sha256=digest(prompt))
            stage = 'native'
            process = subprocess.run(command, capture_output=True, timeout=max(.001, 30 - (time.monotonic() - started)),
                cwd=runtime, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            (root / 'stdout.bin').write_bytes(process.stdout); (root / 'stderr.log').write_bytes(process.stderr)
            row['returncode'] = process.returncode
            if process.returncode:
                raise ValueError('native_exit')
            inputs, outputs = token_usage(process.stderr, 512)
            final, eog, reasoning = completion_text(process.stdout, thinking=mode == 'thinking')
            row.update(input_tokens=inputs, output_tokens=outputs, finish='eog' if eog else 'without_eog',
                reasoning_chars=reasoning, final_text=final)
            outcome = judge_generated(case['expected'], final, payload, aliases)
        except subprocess.TimeoutExpired as error:
            (root / (stage + '-partial-stdout.bin')).write_bytes(error.stdout or b'')
            (root / (stage + '-partial-stderr.log')).write_bytes(error.stderr or b'')
            outcome = {'state': 'unobserved', 'reason': 'timeout', 'unsupported_answer': None}
            row['metering_incomplete'] = True
        except (ValueError, subprocess.CalledProcessError) as error:
            outcome = {'state': 'invalid', 'reason': str(error), 'unsupported_answer': None}
        row.update(raw_judgment=outcome, stage=stage, elapsed_ms=(time.monotonic() - started) * 1000)
        rows.append(row)
        (output / 'rows.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'case_id': case['id'], 'judgment': outcome}, ensure_ascii=True), flush=True)
    totals = {state: sum(row['raw_judgment']['state'] == state for row in rows) for state in ('passed', 'failed', 'invalid', 'unobserved')}
    positives = sum(row['expected_answerable'] and row['raw_judgment']['state'] == 'passed' for row in rows)
    wrong = sum(row['raw_judgment'].get('unsupported_answer') is True for row in rows)
    result = {'cases_sha256': cases_hash, 'profile': spec, 'mode': mode, 'seed': seed, 'identity': identity,
        'native_template_sha256': hashlib.sha256(template.encode()).hexdigest(), 'rows': rows, 'raw_totals': totals,
        'answerable_passed': positives, 'unsupported_answers': wrong,
        'quality_screen': totals['passed'] >= 8 and positives >= 5 and wrong == 0 and totals['invalid'] == totals['unobserved'] == 0,
        'collector_unchanged': all(digest(HERE / name) == value for name, value in identity.items() if name not in {'node', 'protocol'})
            and digest(protocol) == identity['protocol'],
        'scope': 'Frozen synthetic evidence only; temporary prompt/log files; no memory DB, tools or production profile. Resource screen requires separate owned-tree monitor.',
        'network': 'Explicit --offline, local paths, inherited LLAMA_ARG_* and HF_TOKEN removed. Network attempts not instrumented.',
        'admission': 'pending'}
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'output', 'node', 'jinja'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--mode', choices=('greedy', 'sampling', 'thinking'), default='greedy')
    parser.add_argument('--seed', type=int, default=12009)
    args = parser.parse_args()
    collect(args.assets, args.profile, args.output, args.node, args.jinja, mode=args.mode, seed=args.seed)
