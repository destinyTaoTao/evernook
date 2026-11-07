import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import test from 'node:test';
import { loop, Synthetic } from './loop.mjs';

test('termination during saving leaves readable pending evidence and never auto-replays side effects', { timeout: 15000 }, async () => {
  const child = fork(new URL('./crash-worker.mjs', import.meta.url), [], { stdio: ['ignore', 'ignore', 'pipe', 'ipc'] });
  const exited = new Promise(resolve => child.once('exit', resolve));
  let errorText = '';
  child.stderr.on('data', data => { errorText += data.toString(); });
  const timer = setTimeout(() => child.kill(), 10000);
  try {
    const state = await Promise.race([
      new Promise(resolve => child.once('message', resolve)),
      exited.then(() => { throw new Error('Synthetic worker exited before saving: ' + errorText); }),
    ]);
    child.kill(); await exited;
    clearTimeout(timer);
    const host = await loop(new Synthetic(), state.root);
    try {
      const before = await host.read(state.id);
      assert.equal(before.filter(e => e.type === 'tool/call').length, 1);
      assert.equal(before.filter(e => e.type === 'tool/result').length, 0);
      await assert.rejects(host.resume(state.id), /evernook:tool_publication_failed/);
      assert.deepEqual(await host.read(state.id), before);
      assert.equal(host.adapter.requests.length, 0);
      assert.equal(await readFile(join(state.root, 'synthetic-effect.txt'), 'utf8'), 'synthetic side effect happened once');
    } finally { await host.close(); }
  } finally { clearTimeout(timer); if (child.exitCode === null && child.signalCode === null) child.kill(); await exited; }
});
