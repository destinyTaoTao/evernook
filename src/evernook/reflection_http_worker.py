"""Bounded stdlib HTTP transport, launched with -I -S. No logs, redirects or proxies."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


def main() -> None:
    result: dict[str, object] = {'status': 'transport_error'}
    try:
        data = sys.stdin.buffer.read(262145)
        if len(data) > 262144:
            raise ValueError
        request = json.loads(data)
        body = json.dumps(request['body'], ensure_ascii=False, allow_nan=False).encode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        target = urllib.request.Request(request['url'], data=body, method='POST',
            headers={'Authorization': 'Bearer ' + request['key'], 'Content-Type': 'application/json', 'Accept': 'application/json'})
        with opener.open(target, timeout=request['timeout']) as response:
            raw = response.read(131073)
            if response.status != 200 or len(raw) > 131072:
                raise ValueError
            result = {'status': 'ok', 'body': raw.decode('utf-8', errors='strict')}
    except urllib.error.HTTPError:
        result = {'status': 'http_error'}
    except Exception:
        # Never expose response bodies, key, URL, request data or exception text.
        pass
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
