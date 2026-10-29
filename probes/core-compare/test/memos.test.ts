import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { openMemos } from '../src/memos.ts';

test('real MemOS public lifecycle exposes the original source through getTrace', async () => {
  const root = await mkdtemp(join(tmpdir(), 'evernook-memos-unit-'));
  const memos = await openMemos(root, undefined);
  try {
    const source = '合成用户小林喜欢茶，预算 37 元。\n原文🙂';
    const routed = await memos.core.prepareTurn!({ agent: 'evernook-r0', sessionId: 'synthetic-exact', turnKey: 'synthetic-001', userText: source, ts: 1609459200000 });
    const result = await memos.core.onTurnEnd({ agent: 'evernook-r0', ...routed, agentText: '合成确认。', toolCalls: [], ts: 1609459200001 });
    assert.ok(result.traceId);
    const trace = await memos.core.getTrace(result.traceId);
    assert.equal(trace?.userText, source);
    assert.equal(trace?.agentText, '合成确认。');
  } finally { await memos.core.shutdown(); }
});
