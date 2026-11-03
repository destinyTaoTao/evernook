// Synthetic process boundary: killed by dsh.test.ts after actual native publication.
import { join } from 'node:path';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LlmRuntime, { LlmAdapter, MessageId, freezeMessage, type StreamChunk } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import { installMemory, type Config } from '../src/dsh.ts';

const [serialized, root] = process.argv.slice(2);
const config = JSON.parse(serialized) as Config;
const ctx = new Context();
await ctx.plugin(SessionStore).await();
await ctx.plugin(SessionProjectionRegistry).await();
await ctx.plugin(AgentRegistry).await();
await ctx.plugin(LlmRuntime).await();
await ctx.plugin(ToolRuntime).await();
await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
await ctx.plugin(persistence, { root: join(root, 'sessions'), compression: 'none' }).await();
await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 }).await();
class Paused extends LlmAdapter {
  async *stream(): AsyncIterable<StreamChunk> {
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: '已显示🙂pass' };
    await new Promise(() => {});
  }
}
ctx.llm.registerAdapter(['synthetic'], new Paused());
await installMemory(ctx, config);
ctx.on('agent/assistant-stream', ({ frame }) => {
  if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') process.send?.({ text: frame.chunk.text });
});
ctx.on('agent/error', () => process.send?.({ error: 'synthetic-worker-failed' }));
const handle = await ctx.agents.create({ sessionId: SessionId('durable-killed'), meta: { cwd: root, delegationDepth: 0 },
  agentOptions: { provider: 'synthetic', model: 'local-test' } });
handle.agent.followup(freezeMessage({ id: MessageId('kill-input'), role: 'user', source: { kind: 'user' },
  content: [{ type: 'text', text: '合成：进程退出恢复' }] }));
