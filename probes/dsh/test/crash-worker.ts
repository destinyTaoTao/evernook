import { appendFileSync } from 'node:fs';
import { join } from 'node:path';
import { LlmAdapter, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { openProbe, userInput } from '../src/host.ts';
import { installDisplayBarrier } from '../src/display-barrier.ts';

const [root, stopAt] = process.argv.slice(2);
async function checkpoint(stage: string, details: unknown = {}) {
  appendFileSync(join(root, 'checkpoints.jsonl'), JSON.stringify({ stage, details }) + '\n');
  if (stage !== stopAt) return;
  process.send?.({ stage });
  await new Promise<void>(() => { setInterval(() => {}, 1000); });
}
class CrashAdapter extends LlmAdapter {
  async *stream(_options: GenerateOptions): AsyncIterable<StreamChunk> {
    await checkpoint('provider-start');
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: '未完成合成片段：部分流。' };
    if (stopAt === 'displayed-durable') await new Promise<void>(() => { setInterval(() => {}, 1000); });
    await checkpoint('partial-stream');
    throw new Error('crash checkpoint was not selected');
  }
}
const probe = await openProbe(root, new CrashAdapter(), async point => {
  if (point.captureStage === 'before-provider') await checkpoint(point.stage, point);
});
if (stopAt === 'displayed-durable') {
  installDisplayBarrier(probe.ctx, probe.evidence);
  probe.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') void checkpoint('displayed-durable');
  });
}
const handle = await probe.create(SessionId('synthetic-crash'));
handle.agent.followup(userInput('crash-input-001', '合成崩溃输入：37 元，原文🙂\n第二行。'));
await handle.agent.whenIdle();
await handle.dispose();
await probe.close();
throw new Error('worker reached the end without a crash');
