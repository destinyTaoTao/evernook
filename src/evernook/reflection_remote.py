"""Explicit opt-in OpenAI-compatible reflection, sharing the existing evidence contract."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .extraction import LocalQwen
from .models import MemoryFailure
from .reflection import ModelTurn, _REMOTE_EGRESS
from .reflection_protocol import build_messages, translate
from .runtime import _json, _unique_object


def _contains_secret(value: object, secret: str) -> bool:
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if secret in item:
                return True
        elif isinstance(item, (dict, list, tuple)) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            else:
                pending.extend(item)
    return False


class RemoteReflectionModel:
    def __init__(self, config: dict[str, Any]):
        fields = {'base_url', 'model', 'api_key_env', 'allowed_clients', 'allow_loopback_http', 'output_limit', 'pricing', 'max_cost_micro_usd'}
        if not isinstance(config, dict) or not fields <= set(config) <= fields | {'input_tokens', 'output_tokens'}:
            raise MemoryFailure('invalid_reflection_config')
        try:
            endpoint = urlsplit(config['base_url'])
            if (not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                    or type(config['allow_loopback_http']) is not bool
                    or endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and config['allow_loopback_http']
                        and endpoint.hostname in {'127.0.0.1', '::1'})
                    or any(ord(char) < 33 for char in config['base_url'])
                    or not re.fullmatch(r'[A-Za-z0-9_./:@-]{1,160}', config['model'])
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', config['api_key_env'])
                    or config['output_limit'] not in {'max_completion_tokens', 'max_tokens'}
                    or not isinstance(config['allowed_clients'], list) or not 1 <= len(config['allowed_clients']) <= 32
                    or any(not isinstance(client, str) or not 1 <= len(client) <= 256 or any(ord(c) < 32 for c in client)
                        for client in config['allowed_clients'])
                    or type(config['max_cost_micro_usd']) is not int or not 1 <= config['max_cost_micro_usd'] <= 1000000000):
                raise ValueError
            # Validate the port even when it is not otherwise needed.
            endpoint.port
            if any(type(config.get(name, default)) is not int or not 1 <= config.get(name, default) <= maximum
                    for name, default, maximum in (('input_tokens', 4096, 16384), ('output_tokens', 512, 4096))):
                raise ValueError
            prices = config['pricing']
            if (not isinstance(prices, dict) or set(prices) != {'input_micro_usd_per_million', 'output_micro_usd_per_million', 'request_micro_usd'}
                    or any(type(value) is not int or not 0 <= value <= 10 ** 12 for value in prices.values())):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MemoryFailure('invalid_reflection_config') from None
        self.config = json.loads(_json(config))
        self.allowed_clients = frozenset(config['allowed_clients'])
        self.max_cost_micro_usd: int = config['max_cost_micro_usd']
        self.turn_input_tokens: int = config.get('input_tokens', 4096)
        self.turn_output_tokens: int = config.get('output_tokens', 512)
        self.worker = Path(__file__).with_name('reflection_http_worker.py')
        self.protocol = Path(__file__).with_name('reflection_protocol.py')
        self.worker_digest = hashlib.sha256(self.worker.read_bytes()).hexdigest()
        self.protocol_digest = hashlib.sha256(self.protocol.read_bytes()).hexdigest()
        self.processor_version = 'openai-compatible-reflection-experimental-v1:' + hashlib.sha256(
            _json([config, self.worker_digest, self.protocol_digest]).encode()).hexdigest()

    def reserve_cost(self, input_tokens: int, output_tokens: int) -> int:
        prices = self.config['pricing']
        return int((input_tokens * prices['input_micro_usd_per_million'] + 999999) // 1000000
            + (output_tokens * prices['output_micro_usd_per_million'] + 999999) // 1000000 + prices['request_micro_usd'])

    def turn(self, payload: dict[str, Any], *, input_tokens: int, output_tokens: int,
             timeout: float, cancel: threading.Event | None) -> ModelTurn:
        if payload.get('_remote_egress') is not _REMOTE_EGRESS:
            raise MemoryFailure('remote_reflection_not_permitted')
        deadline = time.monotonic() + timeout
        def remaining() -> float:
            if cancel is not None and cancel.is_set():
                raise MemoryFailure('reflection_canceled')
            left = deadline - time.monotonic()
            if left <= 0:
                raise MemoryFailure('reflection_timeout')
            return left
        remaining()
        key = os.environ.get(self.config['api_key_env'], '')
        if not key or len(key) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise MemoryFailure('reflection_remote_credentials_unavailable')
        if _contains_secret(payload, key):
            raise MemoryFailure('protected_query')
        messages = build_messages(payload)
        # Capability metadata, not new semantic instructions or extra source access.
        packet = json.loads(messages[-1]['content'], object_pairs_hook=_unique_object)
        packet['allowed_actions'] = ['answer', 'abstain', 'clarify', 'refuse']
        messages[-1]['content'] = _json(packet)
        if _contains_secret(messages, key) or key in self.config['base_url']:
            raise MemoryFailure('protected_query')
        # Conservative portable preflight, not an exact tokenizer for arbitrary vendors.
        estimate = sum(len(message['content'].encode()) + 64 for message in messages) + 64
        if estimate > input_tokens:
            raise MemoryFailure('reflection_input_budget')
        if (hashlib.sha256(self.worker.read_bytes()).hexdigest() != self.worker_digest
                or hashlib.sha256(self.protocol.read_bytes()).hexdigest() != self.protocol_digest):
            raise MemoryFailure('reflection_integrity_mismatch')
        body = {'model': self.config['model'], 'messages': messages, 'stream': False, 'store': False,
            'n': 1, self.config['output_limit']: output_tokens}
        if _contains_secret(body, key):
            raise MemoryFailure('protected_query')
        request = _json({'url': self.config['base_url'].rstrip('/') + '/chat/completions', 'key': key,
            'body': body, 'timeout': remaining()}).encode()
        if len(request) > 262144:
            raise MemoryFailure('reflection_input_budget')
        environment = {name: value for name, value in os.environ.items() if name.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'SYSTEMDRIVE'}}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen([sys.executable, '-I', '-S', str(self.worker)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            pending: bytes | None = request
            while True:
                try:
                    raw, _ = process.communicate(input=pending, timeout=min(.1, remaining()))
                    break
                except subprocess.TimeoutExpired:
                    pending = None
            remaining()
            if process.returncode or len(raw) > 1048576:
                raise MemoryFailure('reflection_remote_unavailable')
            envelope = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
            if envelope.get('status') != 'ok':
                raise MemoryFailure('reflection_remote_unavailable')
            value = json.loads(envelope['body'], object_pairs_hook=_unique_object)
            usage = value.get('usage')
            if (not isinstance(usage, dict) or any(type(usage.get(name)) is not int or usage[name] < 1
                    for name in ('prompt_tokens', 'completion_tokens'))):
                raise MemoryFailure('reflection_remote_usage_unavailable')
            inputs, outputs = usage['prompt_tokens'], usage['completion_tokens']
            if (inputs > input_tokens or outputs > output_tokens or 'total_tokens' in usage
                    and (type(usage['total_tokens']) is not int or usage['total_tokens'] != inputs + outputs)):
                raise MemoryFailure('reflection_token_budget')
            cost = self.reserve_cost(inputs, outputs)
            choices = value.get('choices')
            error = 'reflection_remote_invalid_output'
            try:
                if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                    choice = choices[0]; message = choice.get('message')
                    if (choice.get('finish_reason') == 'stop' and isinstance(message, dict) and message.get('role') == 'assistant'
                            and isinstance(message.get('content'), str) and not message.get('tool_calls') and not message.get('function_call')
                            and not message.get('refusal') and not _contains_secret(value, key)):
                        selection = LocalQwen.parse_selection(message['content'])
                        if _contains_secret(selection, key):
                            raise MemoryFailure('reflection_remote_invalid_output')
                        action = translate(selection, payload)
                        if _contains_secret(action, key):
                            raise MemoryFailure('reflection_remote_invalid_output')
                        return ModelTurn(action, inputs, outputs, cost_micro_usd=cost)
            except (MemoryFailure, ValueError, TypeError, KeyError, RecursionError):
                pass
            return ModelTurn({}, inputs, outputs, cost_micro_usd=cost, error=error)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise MemoryFailure('reflection_remote_unavailable') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()
