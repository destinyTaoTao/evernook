import assert from 'node:assert/strict';
import { mkdtemp, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { Context, load, Prompt, Tools } from './runtime.mjs';
const { default: Sessions, SessionId } = await load('@deepseek-ai/dsh-session');
const { default: Projection } = await load('@deepseek-ai/dsh-session-projection');
const { default: Agents } = await load('@deepseek-ai/dsh-agent');
const { default: Llm, LlmAdapter, MessageId, ToolCallId, freezeMessage } = await load('@deepseek-ai/dsh-llm');
const { default: Attachments } = await load('@deepseek-ai/dsh-attachment-local');
const persistence = await load('@deepseek-ai/dsh-session-persistence-jsonl');
const { default: AgentLoop } = await load('@deepseek-ai/dsh-agent-loop');

export class Synthetic extends LlmAdapter {
  requests = [];
  names = ['synthetic-tool'];
  arguments = '{}';
  async *stream(options) {
    this.requests.push(options);
    if (this.requests.length === 1) {
      for (const [index, name] of this.names.entries()) {
        const id = ToolCallId('synthetic-' + index);
        const block = { type: 'tool-call', id, name, arguments: this.arguments };
        yield { type: 'block-start', index, blockType: 'tool-call' };
        yield { type: 'tool-call-delta', index, id, name, argumentsDelta: this.arguments };
        yield { type: 'block-end', index, block };
      }
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
    } else {
      yield { type: 'block-start', index: 0, blockType: 'text' };
      yield { type: 'text-delta', index: 0, text: 'synthetic answer after tools' };
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'synthetic answer after tools' } };
      yield { type: 'finish', reason: { kind: 'stop' } };
    }
  }
}

export async function loop(adapter = new Synthetic(), existing) {
  assert.ok(process.env.EVERNOOK_DSH_TEST_OUTPUT, 'Set external synthetic-only EVERNOOK_DSH_TEST_OUTPUT');
  await mkdir(process.env.EVERNOOK_DSH_TEST_OUTPUT, { recursive: true });
  const root = existing ?? await mkdtemp(join(process.env.EVERNOOK_DSH_TEST_OUTPUT, 'loop-'));
  const ctx = new Context();
  await ctx.plugin(Sessions).await();
  await ctx.plugin(Projection).await();
  await ctx.plugin(Agents).await();
  await ctx.plugin(Llm).await();
  await ctx.plugin(Attachments, { dshHome: join(root, 'home') }).await();
  await ctx.plugin(Tools).await();
  await ctx.plugin(Prompt, { includeHarnessIdentity: false }).await();
  await ctx.plugin(persistence, { root: join(root, 'sessions'), compression: 'none' }).await();
  await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 2 }).await();
  ctx.llm.registerAdapter(['synthetic'], adapter);
  const errors = [];
  ctx.on('agent/error', ({ error }) => { errors.push(error); });
  return { ctx, root, adapter, errors,
    create: () => ctx.agents.create({ sessionId: SessionId(randomUUID()), meta: { cwd: root, delegationDepth: 0 },
      agentOptions: { provider: 'synthetic', model: 'local-test' } }),
    resume: id => ctx.agents.resume({ resumeSessionId: id, agentOptions: { provider: 'synthetic', model: 'local-test' } }),
    async read(id) { const handle = await ctx.sessionPersistence.open(id, 'read'); try { return (await handle.read()).events; } finally { await handle.close(); } },
    close: () => ctx.fiber.dispose(),
  };
}
export function follow(handle, id = randomUUID()) {
  handle.agent.followup(freezeMessage({ id: MessageId(id), role: 'user', source: { kind: 'user' },
    content: [{ type: 'text', text: '合成：只在临时目录执行工具。' }] }));
}
