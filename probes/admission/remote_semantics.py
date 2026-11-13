"""Collect a bounded, synthetic-only remote semantic screen outside the checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from unittest.mock import patch

from evernook.models import MemoryFailure
from evernook.paths import external
from evernook.reflection import _REMOTE_EGRESS
from evernook.reflection_remote import RemoteReflectionModel
from semantic_reflection import judge, judge_generated, load_cases, packet

ROOT = Path(__file__).resolve().parents[2]
SUITE_HASH = 'eaff98c68c7b1144cb7bb7d869c75a097289d78c491a090730496f5933e463b0'


def safe_response(raw: bytes, secret: str) -> str | None:
    """Fail closed on malformed or potentially secret-bearing response content."""
    try:
        envelope = json.loads(raw)
        if envelope.get('status') != 'ok' or not isinstance(envelope.get('body'), str):
            return None
        body: str = envelope['body']
        value = json.loads(body)
        pending: list[Any] = [body, value]
        count = 0
        while pending:
            item = pending.pop()
            count += 1
            if count > 100000:
                return None
            if isinstance(item, str):
                if secret in item:
                    return None
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    continue
                if isinstance(decoded, (dict, list, str)) and decoded != item:
                    pending.append(decoded)
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return body
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def collect(output: Path, config: dict[str, Any], total_budget: int, *, output_tokens: int = 512) -> dict[str, Any]:
    cases, digest = load_cases(ROOT / 'eval/zh-reflection-semantics-v1')
    if digest != SUITE_HASH:
        raise ValueError('unexpected_frozen_suite')
    model = RemoteReflectionModel(config)
    secret = os.environ.get(config['api_key_env'], '')
    if len(secret) < 8:
        raise ValueError('credential_unavailable')
    if type(output_tokens) is not int or not 1 <= output_tokens <= model.turn_output_tokens:
        raise ValueError('screen_output_budget')
    reservation = model.reserve_cost(4096, output_tokens)
    if reservation > model.max_cost_micro_usd or reservation * len(cases) > total_budget:
        raise ValueError('screen_cost_budget')
    destination = external(output)
    destination.mkdir()
    rows: list[dict[str, Any]] = []
    for case in cases:
        payload, aliases = packet(case)
        payload['_remote_egress'] = _REMOTE_EGRESS
        captured: list[str] = []
        class TraceProcess(subprocess.Popen[bytes]):
            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                value, error = super().communicate(input=input, timeout=timeout)
                if value:
                    safe = safe_response(value, secret)
                    if safe is not None:
                        captured.append(safe)
                return value, error
        start = time.monotonic()
        try:
            with patch('evernook.reflection_remote.subprocess.Popen', TraceProcess):
                turn = model.turn(payload, input_tokens=4096, output_tokens=output_tokens, timeout=30, cancel=None)
            row: dict[str, Any] = {'id': case['id'], 'category': case['category'], 'action': turn.action,
                'error': turn.error, 'input_tokens': turn.input_tokens, 'output_tokens': turn.output_tokens,
                'cost_micro_usd': turn.cost_micro_usd,
                'judgment': judge(case['expected'], turn.action, aliases) if not turn.error else {'state': 'invalid', 'reason': turn.error}}
        except MemoryFailure as error:
            row = {'id': case['id'], 'category': case['category'], 'error': error.code,
                'judgment': {'state': 'unobserved'}, 'usage': 'unknown'}
        row['elapsed_ms'] = round((time.monotonic() - start) * 1000)
        row['reserved_micro_usd'] = reservation
        row['raw_judgment'] = {'state': 'unobserved'}
        if captured:
            body = captured[0]
            path = destination / (case['id'] + '.response.json')
            path.write_bytes(body.encode('utf-8'))
            response = json.loads(body)
            row['response_sha256'] = hashlib.sha256(body.encode('utf-8')).hexdigest()
            row['response_model'] = response.get('model')
            row['system_fingerprint'] = response.get('system_fingerprint')
            choices = response.get('choices')
            if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                choice = choices[0]
                row['finish_reason'] = choice.get('finish_reason')
                message = choice.get('message')
                if choice.get('finish_reason') == 'stop' and isinstance(message, dict):
                    row['raw_judgment'] = judge_generated(case['expected'], message.get('content'), payload, aliases)
        rows.append(row)
        result = {'suite_sha256': digest, 'processor': model.processor_version, 'model_requested': config['model'],
            'budget': {'seconds': 30, 'input_tokens': 4096, 'output_tokens': output_tokens,
                'total_reserved_micro_usd': reservation * len(rows), 'total_cap_micro_usd': total_budget},
            'rows': rows, 'admission': 'pending'}
        (destination / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(case['id'], row['judgment']['state'], row['elapsed_ms'], flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--output-tokens', type=int, choices=[512, 1024], default=512)
    args = parser.parse_args()
    collect(args.output, {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-v4-pro',
        'api_key_env': 'DEEPSEEK_API_KEY', 'allowed_clients': ['synthetic'], 'allow_loopback_http': False,
        'output_limit': 'max_tokens', 'pricing': {'input_micro_usd_per_million': 1320000,
            'output_micro_usd_per_million': 3960000, 'request_micro_usd': 0},
        'output_tokens': args.output_tokens, 'max_cost_micro_usd': 10000},
        total_budget=100000, output_tokens=args.output_tokens)
