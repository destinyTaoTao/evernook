import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { MinimalStore } from '../src/minimal.ts';

test('minimal comparison path preserves exact source text and rejects conflicting retries', async () => {
  const root = await mkdtemp(join(tmpdir(), 'evernook-core-unit-'));
  const store = new MinimalStore(root);
  const message = { id: 'synthetic-source-1', text: '茶：37 元。\n合成🙂', timestamp: '2021-04-05T00:00:00Z' };
  try {
    store.put(message, [1, 0]);
    store.put(message, [1, 0]);
    assert.deepEqual(store.get(message.id), message);
    assert.throws(() => store.put({ ...message, text: 'different' }, [1, 0]), /conflict/);
    assert.deepEqual(store.get(message.id), message);
  } finally { store.close(); }
  const reopened = new MinimalStore(root);
  try { assert.deepEqual(reopened.get(message.id), message); }
  finally { reopened.close(); }
});

test('a one-character query can retrieve an old source among recent unrelated messages after rebuild', async () => {
  const root = await mkdtemp(join(tmpdir(), 'evernook-core-unit-'));
  const store = new MinimalStore(root);
  const old = { id: 'old', text: '合成：喜欢茶。', timestamp: '2021-04-05T00:00:00Z' };
  try {
    store.put(old, [1, 0]);
    store.put({ id: 'recent', text: '合成：今天整理椅子。', timestamp: '2026-10-28T00:00:00Z' }, [0, 1]);
    assert.equal(store.search('茶', [1, 0], 1)[0]?.id, old.id);
    store.rebuild([{ id: 'old', vector: [1, 0] }, { id: 'recent', vector: [0, 1] }]);
    assert.equal(store.search('茶', [1, 0], 1)[0]?.id, old.id);
    assert.deepEqual(store.get(old.id), old);
  } finally { store.close(); }
});
