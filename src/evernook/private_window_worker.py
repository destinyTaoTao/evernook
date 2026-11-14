"""Versioned host-owned private window entry point with mandatory lifetime pipe."""
from __future__ import annotations

import json
import os
import sys

from .worker import main


if __name__ == '__main__':
    if (os.environ.get('EVERNOOK_WORKER_LIFETIME_PIPE') != '1'
            or len(sys.argv) < 2 or sys.argv[1] not in ('service-private-window', 'service-private-session')):
        sys.stdout.write(json.dumps({'contract_version': 1, 'ok': False,
            'error': {'code': 'worker_lifetime_unavailable'}}) + '\n')
        raise SystemExit(1)
    main()
