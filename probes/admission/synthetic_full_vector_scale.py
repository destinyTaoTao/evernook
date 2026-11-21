"""Controlled index stress test with synthetic text fingerprints, never MiniLM embeddings."""
from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import struct
import threading
import time
from typing import Any

from evernook import MemoryRuntime, SemanticRecall
from evernook import embedding, recall_index, runtime as runtime_module, semantic, vector_distance
from evernook.embedding import EmbeddingSpace
from evernook.models import MemoryFailure
from evernook.paths import external, provision_private_directory
from evernook.semantic import _ELIGIBLE


COUNT = 1_000_000
POLICY = Path(__file__).with_name('policy.json')
TOPICS = ('湖边步道观察记录', '厨房食材库存记录', '天文摄影曝光记录', '编辑器快捷键练习',
    '图书馆书架整理记录', '公交时刻抄录练习', '花园植物浇水记录', '散步路线观察笔记')
ALGORITHM = b'BLAKE2s-16(utf8); block i has signed 0.25 at 24*i+(byte&127)%24; others zero; little-endian float32'
SPACE = EmbeddingSpace('synthetic-text-fingerprint-384', 'v1', 384, hashlib.sha256(ALGORITHM).hexdigest())
POSITIVE, NEGATIVE = struct.pack('<f', 0.25), struct.pack('<f', -0.25)


def fingerprint(text: str) -> bytes:
    blob = bytearray(384 * 4)
    for block, byte in enumerate(hashlib.blake2s(text.encode(), digest_size=16).digest()):
        start = (block * 24 + (byte & 127) % 24) * 4
        blob[start:start + 4] = NEGATIVE if byte & 128 else POSITIVE
    return bytes(blob)


class SyntheticFingerprints:
    space = SPACE

    def embed(self, texts: tuple[str, ...], *, timeout: float, cancel: threading.Event | None = None) -> tuple[tuple[float, ...], ...]:
        if timeout <= 0 or cancel is not None and cancel.is_set():
            raise MemoryFailure('synthetic_embedding_canceled')
        return tuple(struct.unpack('<384f', fingerprint(text)) for text in texts)


def save(path: Path, value: Any) -> None:
    with path.open('x', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write('\n')


def copy_runtime(source: Path, target: Path) -> None:
    provision_private_directory(target)
    for name in ('client.json', 'idempotency.key', 'mcp-access-scale-writer.json', 'mcp-access-scale-maintenance.json'):
        shutil.copy2(source / name, target / name)
    config = json.loads((source / 'runtime.json').read_bytes())
    save(target / 'runtime.json', {**config, 'data_dir': str(target)})
    with closing(sqlite3.connect((source / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(target / 'memory.sqlite3')) as clone:
            original.backup(clone)


def corpus_rows(db: sqlite3.Connection, first_text: str) -> Iterator[tuple[str, str, int, int]]:
    seen = bytearray(COUNT)
    for row in db.execute(f'''SELECT messages.id,event,messages.receipt,coalesce(v.version,1) FROM messages
            LEFT JOIN visibility v ON v.message_id=messages.id WHERE {_ELIGIBLE} ORDER BY messages.id'''):
        source, receipt = json.loads(row[1]), json.loads(row[2])
        n = source['sequence']
        if type(n) is not int or not 0 <= n < COUNT or seen[n]:
            raise ValueError('unexpected_scale_inventory')
        seen[n] = 1
        expected = first_text if n == 0 else f'合成条目{n:07d}：{TOPICS[n % 8]}；保留数字37.50与🙂。'
        if (source['text'] != expected or source['role'] != 'user' or source['completion'] != 'complete'
                or source['source'] != {'entry': 'synthetic-scale', 'session': 'zh-scale-v1', 'branch': 'main', 'event': f'm{n:07d}'}
                or receipt['message_id'] != row[0] or len(expected) > SPACE.chunk_size):
            raise ValueError('unexpected_synthetic_source')
        yield row[0], expected, row[3], receipt['content_version']
    if seen.count(1) != COUNT:
        raise ValueError('incomplete_eligible_inventory')


def prepare(source: Path, output: Path) -> None:
    manifest = json.loads((source.parent / 'manifest.json').read_bytes())
    if (manifest['expected_messages'] != COUNT or manifest['policy_sha256'] != hashlib.sha256(POLICY.read_bytes()).hexdigest()):
        raise ValueError('unexpected_ingestion_manifest')
    output.mkdir()
    target = output / 'runtime'
    started = time.perf_counter()
    copy_runtime(source, target)
    after_copy = time.perf_counter()
    path = target / 'memory.sqlite3'
    key = SPACE.key
    first_text = json.loads(POLICY.read_bytes())['scale']['pure_semantic_original']
    with closing(sqlite3.connect(path)) as db:
        if db.execute('SELECT count(*) FROM messages').fetchone()[0] != COUNT:
            raise ValueError('unexpected_message_count')
        SemanticRecall._schema(db)
        db.execute('INSERT INTO semantic_spaces_v1 VALUES (?,?)', (key, json.dumps(asdict(SPACE), sort_keys=True, separators=(',', ':'))))
        documents, chunks = [], []
        done = 0
        source_hash = hashlib.sha256()
        for message_id, text, version, content_version in corpus_rows(db, first_text):
            source_hash.update(json.dumps([message_id, text, version, content_version], ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
            documents.append((key, message_id, version, content_version, len(text), 1))
            chunks.append((key, message_id, 0, len(text), version, content_version, fingerprint(text)))
            done += 1
            if len(chunks) == 4000 or done == COUNT:
                db.executemany('INSERT INTO semantic_documents_v1 VALUES (?,?,?,?,?,?)', documents)
                db.executemany('INSERT INTO semantic_chunks_v1 VALUES (?,?,?,?,?,?,?)', chunks)
                db.commit()
                documents.clear()
                chunks.clear()
            if done % 100000 == 0:
                print('prepared', done, flush=True)
        after_build = time.perf_counter()
        db.create_function('expected_vector', 1, fingerprint, deterministic=True)
        # Every row must round-trip from the source and independently match the stored vector bytes.
        verified = db.execute(f'''SELECT count(*) FROM semantic_documents_v1 d
            JOIN messages ON messages.id=d.message_id LEFT JOIN visibility v ON v.message_id=messages.id
            JOIN semantic_chunks_v1 c ON c.space_id=d.space_id AND c.message_id=d.message_id
            WHERE d.space_id=? AND {_ELIGIBLE} AND d.state_version=coalesce(v.version,1)
            AND d.content_version=json_extract(messages.receipt,'$.content_version') AND d.chunk_count=1
            AND d.text_length=length(json_extract(messages.event,'$.text')) AND c.start=0 AND c.end=d.text_length
            AND c.content_version=d.content_version AND c.state_version=d.state_version
            AND c.vector=expected_vector(json_extract(messages.event,'$.text'))''', (key,)).fetchone()[0]
        counts = {table: db.execute(f'SELECT count(*) FROM {table} WHERE space_id=?', (key,)).fetchone()[0]
            for table in ('semantic_documents_v1', 'semantic_chunks_v1', 'semantic_failures_v1')}
        if verified != COUNT or list(counts.values()) != [COUNT, COUNT, 0]:
            raise ValueError('derived_correspondence_mismatch')
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0] != 0:
            raise ValueError('incomplete_checkpoint')
    runtime = MemoryRuntime(target / 'runtime.json')
    access = runtime.access_from_file(target / 'mcp-access-scale-writer.json')
    recall = SemanticRecall(runtime, SyntheticFingerprints())
    status = recall.status(access)
    if (recall.candidate_index_enabled(access) or status.state != 'ready'
            or (status.source_messages, status.indexed_messages, status.stored_vectors) != (COUNT, COUNT, COUNT)):
        raise ValueError('unexpected_public_baseline')
    save(output / 'prepared.json', {'space': asdict(SPACE), 'source_inventory_sha256': source_hash.hexdigest(),
        'copy_seconds': after_copy - started, 'bulk_build_seconds': after_build - after_copy,
        'verification_seconds': time.perf_counter() - after_build, 'verified': verified, 'counts': counts,
        'checkpoint': checkpoint, 'database_bytes': path.stat().st_size, 'public_status': asdict(status)})


def measure(output: Path) -> None:
    if json.loads((output / 'prepared.json').read_bytes())['verified'] != COUNT:
        raise ValueError('fixture_unverified')
    target = output / 'runtime'
    runtime = MemoryRuntime(target / 'runtime.json')
    recall = SemanticRecall(runtime, SyntheticFingerprints())
    reader = runtime.access_from_file(target / 'mcp-access-scale-writer.json')
    with closing(sqlite3.connect((target / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        if recall_index._definitions(db):
            raise ValueError('baseline_contains_candidate_index_objects')
    as_of = json.loads(POLICY.read_bytes())['scale']['as_of']
    sources = (Path(__file__), POLICY, *(Path(inspect.getfile(module)) for module in
        (runtime_module, semantic, recall_index, vector_distance, embedding)))
    save(output / 'measurement.json', {'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'as_of': as_of,
        'query_timeout_seconds': 120, 'samples_per_query_per_mode': 1, 'order': ['baseline', 'indexed']})
    results: dict[str, Any] = {}
    for mode in ('baseline', 'indexed'):
        if mode == 'indexed':
            started = time.perf_counter()
            maintainer = runtime.access_from_file(target / 'mcp-access-scale-maintenance.json')
            recall.configure_candidate_index(maintainer, enabled=True)
            if not recall.candidate_index_enabled(reader):
                raise ValueError('candidate_indexes_not_verified')
            save(output / 'index-build.json', {'seconds': time.perf_counter() - started,
                'database_bytes': (target / 'memory.sqlite3').stat().st_size})
        for name, query in (('recent', '合成条目0999999'), ('historical', '合成条目0000001'), ('no-answer', '外星深海音乐会许可证')):
            started = time.perf_counter()
            try:
                page = recall.recall(reader, query, limit=10, as_of=as_of, timeout=120)
                elapsed = time.perf_counter() - started
                result: dict[str, Any] = {'page': asdict(page)}
            except MemoryFailure as failure:
                elapsed = time.perf_counter() - started
                result = {'error': failure.code}
            save(output / f'{mode}-{name}.json', {'seconds': elapsed, **result})
            results[f'{mode}-{name}'] = result
            print(mode, name, elapsed, result.get('error'), flush=True)
    save(output / 'comparisons.json', {q: {'equal': results['baseline-' + q] == results['indexed-' + q],
        'both_pages': all('page' in results[mode + '-' + q] for mode in ('baseline', 'indexed'))}
        for q in ('recent', 'historical', 'no-answer')})


def rebuild(output: Path) -> None:
    if not (output / 'comparisons.json').is_file() or (output / 'rebuild.json').exists():
        raise ValueError('rebuild_requires_completed_measurement')
    target = output / 'runtime'
    runtime = MemoryRuntime(target / 'runtime.json')
    maintainer = runtime.access_from_file(target / 'mcp-access-scale-maintenance.json')
    recall = SemanticRecall(runtime, SyntheticFingerprints())
    if not recall.candidate_index_enabled(maintainer):
        raise ValueError('rebuild_requires_valid_candidate_indexes')
    started = time.perf_counter()
    recall.configure_candidate_index(maintainer, enabled=True, rebuild=True)
    elapsed = time.perf_counter() - started
    if not recall.candidate_index_enabled(maintainer):
        raise ValueError('rebuilt_index_definitions_mismatch')
    validated = {}
    with closing(sqlite3.connect((target / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        for name, columns, order in (
                (recall_index.BY_SOURCE, "id,json_extract(receipt,'$.content_version'),occurred,received", 'id'),
                (recall_index.BY_TIME, 'occurred,id', 'occurred DESC,id')):
            paths = {}
            for mode, hint in (('source', 'NOT INDEXED'), ('rebuilt', 'INDEXED BY ' + name)):
                sql = f'SELECT {columns} FROM messages {hint} WHERE {recall_index.ORIGINAL} ORDER BY {order}'
                count = 0
                digest = hashlib.sha256()
                for row in db.execute(sql):
                    digest.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
                    count += 1
                paths[mode] = {'count': count, 'sha256': digest.hexdigest(),
                    'plan': [item[3] for item in db.execute('EXPLAIN QUERY PLAN ' + sql)]}
            if (paths['source']['count'] != COUNT or paths['rebuilt']['count'] != COUNT
                    or paths['source']['sha256'] != paths['rebuilt']['sha256']):
                raise ValueError('rebuilt_projection_mismatch')
            validated[name] = paths
    save(output / 'rebuild.json', {'rebuild_seconds': elapsed, 'projection_check_seconds': time.perf_counter() - started - elapsed,
        'definitions_verified': True, 'projections': validated})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'measure', 'rebuild'))
    parser.add_argument('--source', type=Path, help='Original synthetic ingestion runtime, read-only')
    parser.add_argument('--output', type=Path, required=True, help='Fresh directory outside the repository for prepare')
    args = parser.parse_args()
    if args.mode == 'prepare':
        if args.source is None:
            parser.error('prepare requires --source')
        prepare(external(args.source), external(args.output))
    elif args.mode == 'measure':
        measure(external(args.output))
    else:
        rebuild(external(args.output))
