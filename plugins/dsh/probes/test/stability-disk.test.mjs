import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import { join } from 'node:path';
import { disk } from '../stability-disk.mjs';

assert.ok(process.env.EVERNOOK_TEST_OUTPUT);

test('live disk traversal counts retained files when a real journal vanishes after enumeration', async t => {
  await fs.mkdir(process.env.EVERNOOK_TEST_OUTPUT, { recursive: true });
  const root = await fs.mkdtemp(join(process.env.EVERNOOK_TEST_OUTPUT, 'disk-'));
  const journal = join(root, 'memory.sqlite3-wal');
  await fs.writeFile(journal, 'synthetic transient journal');
  await fs.writeFile(join(root, 'retained.txt'), '12345');
  await fs.mkdir(join(root, 'nested'));
  await fs.writeFile(join(root, 'nested', 'retained.txt'), '123');
  const actual = fs.lstat;
  let removed = false;
  t.mock.method(fs, 'lstat', async path => {
    if (path === journal) { await fs.unlink(journal); removed = true; }
    return actual(path);
  });
  assert.equal(await disk(root), 8);
  assert.equal(removed, true);
});

test('disk traversal still fails on permission errors', async t => {
  const root = await fs.mkdtemp(join(process.env.EVERNOOK_TEST_OUTPUT, 'disk-denied-'));
  await fs.writeFile(join(root, 'retained.txt'), 'synthetic');
  t.mock.method(fs, 'lstat', async () => { throw Object.assign(new Error('synthetic denied'), { code: 'EACCES' }); });
  await assert.rejects(disk(root), { code: 'EACCES' });
});
