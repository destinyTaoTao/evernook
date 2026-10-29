import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { LlmAdapter, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { Evidence, openProbe } from '../src/host.ts';
import { readPersistedChunks } from '../src/display-barrier.ts';

class NeverCalledAdapter extends LlmAdapter {
  async *stream(_options: GenerateOptions): AsyncIterable<StreamChunk> { throw new Error('recovery must not call a model'); }
}

for (const stage of ['host-flushed', 'sink-committed', 'partial-stream', 'displayed-durable']) {
  test(`forced process exit at ${stage}: recover confirmed input and expose lost stream range`, async () => {
    const root = await mkdtemp(join(process.env.EVERNOOK_PROBE_RUN_ROOT ?? tmpdir(), 'evernook-crash-'));
    console.log(`Synthetic crash evidence: ${root}`);
    const child = fork(new URL('./crash-worker.ts', import.meta.url), [root, stage], { silent: true });
    let output = '';
    let checkpointReceived = false;
    child.stdout?.on('data', data => { output += data.toString(); });
    child.stderr?.on('data', data => { output += data.toString(); });
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await new Promise<void>((resolve, reject) => {
        timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`worker checkpoint timeout: ${output}`)); }, 15000);
        child.once('error', reject);
        child.once('message', message => {
          try { assert.deepEqual(message, { stage }); checkpointReceived = true; child.kill('SIGKILL'); }
          catch (error) { reject(error); }
        });
        child.once('exit', (code, signal) => {
          if (checkpointReceived && (signal === 'SIGKILL' || (process.platform === 'win32' && code !== 0))) resolve();
          else reject(new Error(`worker exited before termination (${code}/${signal}): ${output}`));
        });
      });
    } finally {
      clearTimeout(timer);
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      await writeFile(join(root, 'worker-output.txt'), output);
    }
    const checkpoints = await readFile(join(root, 'checkpoints.jsonl'), 'utf8');
    assert.match(checkpoints, new RegExp(`"stage":"${stage}"`));
    const probe = await openProbe(root, new NeverCalledAdapter());
    const recovered = new Evidence(join(root, 'recovered.sqlite'));
    try {
      if (stage === 'host-flushed') assert.equal(probe.evidence.events('synthetic-crash').length, 0);
      else assert.match(JSON.stringify(probe.evidence.events('synthetic-crash')), /合成崩溃输入/);
      await probe.rescan(SessionId('synthetic-crash'), recovered);
      await probe.rescan(SessionId('synthetic-crash'), recovered);
      assert.match(recovered.context(), /合成崩溃输入：37 元，原文🙂/);
      assert.doesNotMatch(JSON.stringify(recovered.events('synthetic-crash')), /未完成合成片段/);
      if (stage === 'displayed-durable') {
        const chunks = readPersistedChunks(root, 'synthetic-crash');
        assert.deepEqual(chunks.map(record => record.ordinal), [0, 1]);
        assert.equal(new Set(chunks.map(record => record.attempt)).size, 1);
        assert.deepEqual(chunks[1].chunk, { type: 'text-delta', index: 0, text: '未完成合成片段：部分流。' });
        assert.equal(chunks.filter(record => record.chunk.type === 'finish').length, 0);
      }
      assert.doesNotMatch(JSON.stringify(recovered.events('synthetic-crash')), /"type":"turn\/end"/);
      // The open turn and absent assistant record are the explicit recovery gap.
      assert.match(JSON.stringify(recovered.events('synthetic-crash')), /"type":"turn\/start"/);
    } finally { recovered.close(); await probe.close(); }
  });
}
