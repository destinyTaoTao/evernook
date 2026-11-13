"""Host-owned, read-only bridge for bounded management fingerprint batches."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import threading
from typing import Any

from .models import MemoryFailure
from .service import ServiceClient, client_access
from .worker import protect_parent_lifetime

MAX_LINE = 131072


def run(config: Path, access_path: Path) -> None:
    """Serve at most four independent requests; malformed framing closes the worker."""
    pending: dict[str, threading.Event] = {}
    guard, output = threading.Lock(), threading.Lock()

    def send(value: dict[str, Any]) -> None:
        raw = (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
        with output:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    def process(request_id: str, requests: list[Any], canceled: threading.Event) -> None:
        try:
            # Reload and verify the access file on every request; no retained authority.
            access = client_access(config, access_path)
            result = ServiceClient(config).call(access, 'cli', {'command': 'manage',
                'input': {'action': 'fingerprints', 'requests': requests}}, timeout=8, cancel=canceled)
            if (not isinstance(result, dict) or set(result) != {'tokens'} or not isinstance(result['tokens'], list)
                    or len(result['tokens']) != len(requests)
                    or any(not isinstance(x, str) or re.fullmatch(r'[0-9a-f]{64}', x) is None for x in result['tokens'])):
                raise MemoryFailure('service_invalid_response')
            send({'id': request_id, 'tokens': result['tokens']})
        except Exception:
            send({'id': request_id, 'error': 'fingerprint_unavailable'})
        finally:
            with guard:
                pending.pop(request_id, None)

    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='evernook-fingerprint') as pool:
            while True:
                raw = sys.stdin.buffer.readline(MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
                    break
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['id']):
                        break
                    request_id = value['id']
                    if set(value) == {'id', 'cancel'} and value['cancel'] is True:
                        with guard:
                            event = pending.get(request_id)
                            if event is not None:
                                event.set()
                        continue
                    if set(value) != {'id', 'requests'} or not isinstance(value['requests'], list) or not 1 <= len(value['requests']) <= 3:
                        break
                    with guard:
                        if request_id in pending:
                            break
                        if len(pending) >= 4:
                            send({'id': request_id, 'error': 'fingerprint_unavailable'})
                            continue
                        event = threading.Event()
                        pending[request_id] = event
                    pool.submit(process, request_id, value['requests'], event)
                except (ValueError, UnicodeError, TypeError):
                    break
    finally:
        with guard:
            for event in pending.values():
                event.set()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--access', required=True, type=Path)
    args = parser.parse_args()
    protect_parent_lifetime()
    run(args.config, args.access)


if __name__ == '__main__':
    main()
