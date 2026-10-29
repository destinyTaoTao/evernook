import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { LlmAdapter, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { openProbe, userInput } from '../src/host.ts';
import { installDisplayBarrier, readPersistedChunks } from '../src/display-barrier.ts';

class SyntheticStream extends LlmAdapter {
  beforeText?: () => void;
  async *stream(_options: GenerateOptions): AsyncIterable<StreamChunk> {
    yield { type: 'block-start', index: 0, blockType: 'text' };
    this.beforeText?.();
    yield { type: 'text-delta', index: 0, text: '合成显示原文🙂\n' };
    yield { type: 'text-delta', index: 0, text: '第二段：37.50 元。' };
    yield { type: 'block-end', index: 0, block: { type: 'text', text: '合成显示原文🙂\n第二段：37.50 元。' } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

test('actual dsh live text publication waits until the same chunk is independently readable', async () => {
  const root = await mkdtemp(join(process.env.EVERNOOK_PROBE_RUN_ROOT ?? tmpdir(), 'evernook-display-'));
  console.log(`Synthetic display evidence: ${root}`);
  const probe = await openProbe(root, new SyntheticStream());
  installDisplayBarrier(probe.ctx, probe.evidence);
  const observations: string[] = [];
  const errors: unknown[] = [];
  probe.ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    if (frame.type !== 'chunk' || frame.chunk.type !== 'text-delta') return;
    const text = frame.chunk.text;
    observations.push(text);
    try {
      const records = readPersistedChunks(root, agent.id);
      assert.ok(records.some(record => record.chunk.type === 'text-delta' && record.chunk.text === text));
    } catch (error) { errors.push(error); }
  });
  const handle = await probe.create(SessionId('synthetic-display'));
  try {
    handle.agent.followup(userInput('display-001', '合成测试：先保存后显示。'));
    await handle.agent.whenIdle();
    assert.deepEqual(observations, ['合成显示原文🙂\n', '第二段：37.50 元。']);
    assert.deepEqual(errors, []);
  } finally { await handle.dispose(); await probe.close(); }
});

test('a failed journal write prevents live text publication instead of displaying unsaved text', async () => {
  const root = await mkdtemp(join(process.env.EVERNOOK_PROBE_RUN_ROOT ?? tmpdir(), 'evernook-display-failure-'));
  console.log(`Synthetic display failure evidence: ${root}`);
  const adapter = new SyntheticStream();
  const probe = await openProbe(root, adapter);
  installDisplayBarrier(probe.ctx, probe.evidence);
  adapter.beforeText = () => { probe.evidence.close(); };
  const displayed: string[] = [];
  const errors: unknown[] = [];
  probe.ctx.on('agent/error', ({ error }) => { errors.push(error); });
  probe.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text);
  });
  const handle = await probe.create(SessionId('synthetic-display-failure'));
  try {
    handle.agent.followup(userInput('display-failure-001', '合成写失败时禁止继续显示。'));
    await handle.agent.whenIdle();
    assert.deepEqual(displayed, []);
    assert.ok(errors.length > 0);
    assert.equal(readPersistedChunks(root, handle.agent.id).filter(record => record.chunk.type === 'text-delta').length, 0);
  } finally { await handle.dispose(); await probe.close(); }
});
