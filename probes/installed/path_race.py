"""Exercise Runtime startup while an owned SQLite writer cycles optional WAL/SHM files."""
from __future__ import annotations

import argparse
from contextlib import closing
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import evernook
from evernook import MemoryFailure, MemoryRuntime, initialize
from evernook.paths import external


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--iterations', type=int, default=100)
    parser.add_argument('--writer', type=Path)
    args = parser.parse_args()
    if args.writer is not None:
        database = external(args.writer)
        root = database.parent.parent
        marker = json.loads((root / 'synthetic-path-race.json').read_text(encoding='utf-8'))
        if marker != {'synthetic': True, 'database': str(database)} or database.parent.name != 'runtime':
            raise ValueError('synthetic_owned_writer_required')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not (root / 'STOP').exists():
            with closing(sqlite3.connect(database, timeout=5)) as db:
                db.execute('UPDATE synthetic_path_race SET n=n+1')
                db.commit()
            time.sleep(.001)
        return 0
    if args.output is None or not 1 <= args.iterations <= 200:
        parser.error('external output and 1..200 iterations required')
    root = external(args.output)
    root.mkdir()
    config, _ = initialize(root / 'runtime', client_id='synthetic', entry='race')
    database = config.parent / 'memory.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE synthetic_path_race(n INTEGER)')
        db.execute('INSERT INTO synthetic_path_race VALUES (0)')
        db.commit()
    (root / 'synthetic-path-race.json').write_text(json.dumps({'synthetic': True, 'database': str(database)}), encoding='utf-8')
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--writer', str(database)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    errors = []
    completed = 0
    started = time.monotonic()
    try:
        for _ in range(args.iterations):
            if child.poll() is not None or time.monotonic() - started >= 110:
                errors.append('owned_writer_or_time_budget_unavailable')
                break
            try:
                MemoryRuntime(config)
            except MemoryFailure as error:
                errors.append(error.code)
            completed += 1
    finally:
        (root / 'STOP').write_text('stop\n', encoding='utf-8')
        try:
            _, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            _, stderr = child.communicate(timeout=5)
            errors.append('owned_writer_stop_timeout')
    if child.returncode != 0 or stderr:
        errors.append('owned_writer_failed')
    with closing(sqlite3.connect(database)) as db:
        writes = db.execute('SELECT n FROM synthetic_path_race').fetchone()[0]
    if writes < 1:
        errors.append('no_concurrent_writes')
    result = {'synthetic': True, 'iterations': completed, 'errors': errors, 'writer_updates': writes,
        'elapsed_seconds': time.monotonic() - started, 'installed_version': importlib.metadata.version('evernook'),
        'import_file': evernook.__file__}
    (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    return 1 if errors or completed != args.iterations else 0


if __name__ == '__main__':
    raise SystemExit(main())
