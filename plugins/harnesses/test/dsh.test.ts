import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import LlmRuntime, { LlmAdapter, MessageId, ToolCallId, freezeMessage, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime, { defineContentToolFixture } from '@deepseek-ai/dsh-tools';
import { installMemory } from '../src/dsh.ts';

const python = process.env.EVERNOOK_TEST_PYTHON;
if (!python) throw new Error('Set EVERNOOK_TEST_PYTHON to the external dev interpreter');
const output = process.env.EVERNOOK_TEST_OUTPUT;
if (!output) throw new Error('Set EVERNOOK_TEST_OUTPUT to an external synthetic-only directory');
const checkout = resolve(import.meta.dirname, '../../..');

class Synthetic extends LlmAdapter {
  requests: GenerateOptions[] = [];
  reply = '合成回复🙂\r\n37.50元。';
  inspect?: (options: GenerateOptions) => void;
  toolOnce = false;
  interrupt = false;
  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    this.requests.push(options);
    this.inspect?.(options);
    if (this.toolOnce && this.requests.length === 1) {
      const id = ToolCallId('synthetic-call');
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'tool-call-delta', index: 0, id, name: 'synthetic-tool', argumentsDelta: '{"amount":37.50}' };
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: '{"amount":37.50}' } };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
      return;
    }
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: this.reply.slice(0, 3) };
    if (this.interrupt) throw new Error('synthetic-provider-timeout');
    yield { type: 'text-delta', index: 0, text: this.reply.slice(3) };
    yield { type: 'block-end', index: 0, block: { type: 'text', text: this.reply } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

async function boot(adapter: Synthetic) {
  await mkdir(output!, { recursive: true });
  const root = await mkdtemp(join(output!, 'native-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; config,access=initialize(Path(sys.argv[1]),client_id='synthetic-dsh',entry='dsh'); path=config.parent/'mcp-access-dsh.json'; pair_client(config,client_id=access.client_id,entry='dsh',access_path=path); print(json.dumps({'config':str(config),'access':str(path)}))";
  const paths = JSON.parse(execFileSync(python!, ['-c', script, join(root, 'memory')], { cwd: checkout, encoding: 'utf8' })) as { config: string; access: string };
  const ctx = new Context();
  await ctx.plugin(SessionStore).await();
  await ctx.plugin(SessionProjectionRegistry).await();
  await ctx.plugin(AgentRegistry).await();
  await ctx.plugin(LlmRuntime).await();
  await ctx.plugin(LocalAttachmentStore, { dshHome: join(root, 'dsh-home') }).await();
  await ctx.plugin(ToolRuntime).await();
  await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
  await ctx.plugin(persistence, { root: join(root, 'sessions'), compression: 'none' }).await();
  await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 }).await();
  ctx.llm.registerAdapter(['synthetic'], adapter);
  const config = { python: python!, config: paths.config, access: paths.access, entry: 'dsh', cwd: checkout };
  const memory = await installMemory(ctx, config);
  const errors: unknown[] = [];
  ctx.on('agent/error', ({ error }) => { errors.push(error); });
  function rows() {
    const db = new DatabaseSync(join(root, 'memory/memory.sqlite3'), { readOnly: true });
    try { return db.prepare('SELECT event,receipt FROM messages').all().map(row => ({ event: JSON.parse(String(row.event)), receipt: JSON.parse(String(row.receipt)) })); }
    finally { db.close(); }
  }
  return { root, ctx, memory, config, errors, rows,
    create: (session: string) => ctx.agents.create({ sessionId: SessionId(session), meta: { cwd: root, delegationDepth: 0 }, agentOptions: { provider: 'synthetic', model: 'local-test' } }),
    async close() { await memory.close(); await ctx.fiber.dispose(); },
  };
}
function user(id: string, text: string) { return freezeMessage({ id: MessageId(id), role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] }); }

test('real native host saves user input before provider and injects sourced evidence as user context in another session', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter);
  try {
    adapter.inspect = () => assert.ok(host.rows().some(row => row.event.role === 'user' && row.event.text === '合成：预算37元。'));
    const first = await host.create('synthetic-one');
    first.agent.followup(user('one', '合成：预算37元。'));
    await first.agent.whenIdle();
    await host.memory.capture(first.agent.session);
    assert.equal(adapter.requests.length, 1);
    await first.dispose();
    const second = await host.create('synthetic-two');
    second.agent.followup(user('two', '预算'));
    await second.agent.whenIdle();
    assert.equal(adapter.requests.length, 2);
    const request = adapter.requests[1];
    assert.match(JSON.stringify(request.messages.filter(message => message.role === 'user')), /untrusted_memory_evidence/);
    assert.match(JSON.stringify(request.messages), /预算37元/);
    assert.doesNotMatch(JSON.stringify(request.messages.filter(message => message.role === 'system')), /untrusted_memory_evidence/);
    await host.memory.capture(second.agent.session);
    const count = host.rows().length;
    await host.memory.rescan(second.agent.session.id);
    await host.memory.rescan(second.agent.session.id);
    assert.equal(host.rows().length, count);
    assert.deepEqual(host.errors, []);
    await second.dispose();
  } finally { await host.close(); }
});

test('provider interruption publishes no buffered prefix and fresh adapter replays durable native records idempotently', async () => {
  const adapter = new Synthetic();
  adapter.interrupt = true;
  const host = await boot(adapter);
  const displayed: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text); });
  const handle = await host.create('synthetic-interrupted');
  let fresh: Awaited<ReturnType<typeof installMemory>> | undefined;
  try {
    handle.agent.followup(user('interrupted-input', '合成：模型中断测试'));
    await handle.agent.whenIdle();
    await host.memory.capture(handle.agent.session);
    assert.deepEqual(displayed, []);
    assert.ok(host.errors.length > 0);
    // The native LLM normalizes adapter throws into a finish(error) frame.
    assert.ok(host.rows().some(row => row.event.role === 'assistant' && row.event.completion === 'failed' && !row.receipt.original_saved));
    await handle.dispose();
    await host.memory.close();
    const count = host.rows().length;
    fresh = await installMemory(host.ctx, host.config);
    await fresh.rescan(SessionId('synthetic-interrupted'));
    await fresh.rescan(SessionId('synthetic-interrupted'));
    assert.equal(host.rows().length, count);
  } finally { await fresh?.close(); await handle.dispose(); await host.close(); }
});

test('each actual dsh live text publication sees its complete saved original in an independent reader', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter);
  const displayed: string[] = [];
  const mismatches: unknown[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type !== 'chunk' || frame.chunk.type !== 'text-delta') return;
    displayed.push(frame.chunk.text);
    try { assert.ok(host.rows().some(row => row.event.role === 'assistant' && row.event.text === adapter.reply && row.receipt.original_saved)); }
    catch (error) { mismatches.push(error); }
  });
  const handle = await host.create('synthetic-display');
  try {
    handle.agent.followup(user('display', '合成：保存后显示'));
    await handle.agent.whenIdle();
    assert.equal(displayed.join(''), adapter.reply);
    assert.deepEqual(mismatches, []);
    assert.deepEqual(host.errors, []);
  } finally { await handle.dispose(); await host.close(); }
});

test('protected output never reaches native live publication or ordinary raw storage', async () => {
  const adapter = new Synthetic();
  adapter.reply = 'password=synthetic-native-output-sentinel-123';
  const host = await boot(adapter);
  const displayed: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text); });
  const handle = await host.create('synthetic-protected-output');
  try {
    handle.agent.followup(user('protected-output', '合成：输出保护测试'));
    await handle.agent.whenIdle();
    assert.deepEqual(displayed, []);
    assert.ok(host.errors.length > 0);
    const paths = await readdir(join(host.root, 'memory'));
    const contents = Buffer.concat(await Promise.all(paths.filter(name => name.startsWith('memory.sqlite3')).map(name => readFile(join(host.root, 'memory', name)))));
    assert.ok(!contents.includes(Buffer.from('synthetic-native-output-sentinel-123')));
  } finally { await handle.dispose(); await host.close(); }
});

test('actual database write failure rejects dispatch before provider invocation', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter);
  const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'));
  db.exec("CREATE TRIGGER synthetic_capture_failure BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'synthetic failure'); END");
  db.close();
  const handle = await host.create('synthetic-storage-failure');
  try {
    handle.agent.followup(user('failure', '合成：失败不能假装保存'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 0);
    assert.equal(host.rows().length, 0);
    assert.ok(host.errors.length > 0);
  } finally { await handle.dispose(); await host.close(); }
});

test('protected input is projection-only and a later safe turn resumes without its raw history after adapter restart', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter);
  const handle = await host.create('synthetic-protected-history');
  let fresh: Awaited<ReturnType<typeof installMemory>> | undefined;
  try {
    handle.agent.followup(user('protected-history', 'password=synthetic-history-dsh-123'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 0);
    assert.ok(host.rows().some(row => row.event.role === 'user' && !row.receipt.original_saved));
    handle.agent.followup(user('safe-after-protected', '合成：后续安全问题'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 1, JSON.stringify(host.errors.map(error => String(error))));
    assert.doesNotMatch(JSON.stringify(adapter.requests[0]), /synthetic-history-dsh-123/);
    assert.equal(host.memory.status(handle.agent.session.id).protected_originals_not_saved, 1);
    assert.equal(host.memory.status(handle.agent.session.id).provider_history, 'current_safe_turn_only');
    assert.ok(host.memory.status(handle.agent.session.id).capture_gaps.some(gap => gap.includes('protected_original_not_saved')));
    assert.doesNotMatch(JSON.stringify(host.rows()), /synthetic-history-dsh-123/);
    await host.memory.close();
    fresh = await installMemory(host.ctx, host.config);
    await fresh.rescan(handle.agent.session.id);
    handle.agent.followup(user('safe-after-restart', '合成：重启后继续安全交流'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 2);
    assert.doesNotMatch(JSON.stringify(adapter.requests[1]), /synthetic-history-dsh-123/);
    assert.equal(fresh.status(handle.agent.session.id).protected_originals_not_saved, 1);
  } finally { await fresh?.close(); await handle.dispose(); await host.close(); }
});

test('native file references and tool events preserve exact text, input links and explicit unsupported gaps on replay', async () => {
  const adapter = new Synthetic();
  adapter.toolOnce = true;
  const host = await boot(adapter);
  host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic local fixture',
    parameters: { amount: { type: 'number' } }, execute: async () => [{ type: 'text', text: '合成结果🙂\r\n37.50元。' }] }));
  const handle = await host.create('synthetic-tools-files');
  try {
    const data = Buffer.from('合成附件🙂\r\n37.50元。');
    const file = await host.ctx.attachments.saveFile({ data, name: 'synthetic.txt' });
    const binary = await host.ctx.attachments.saveFile({ data: Buffer.from([0, 1, 2]), name: 'synthetic.bin' });
    handle.agent.followup(freezeMessage({ id: MessageId('file-input'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '合成：检查附件' }, { type: 'file', attachment: file }, { type: 'file', attachment: binary }] }));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    await host.memory.capture(handle.agent.session);
    const count = host.rows().length;
    await host.memory.rescan(handle.agent.session.id);
    assert.equal(host.rows().length, count);
    const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'), { readOnly: true });
    try {
      const captures = db.prepare('SELECT manifest FROM captures').all().map(row => JSON.parse(String(row.manifest)));
      const input = captures.find(row => row.attachments.some((item: { label: string }) => item.label === 'synthetic.txt'));
      assert.equal(input.complete, false);
      assert.ok(input.attachments.find((item: { label: string }) => item.label === 'synthetic.txt').saved);
      assert.equal(input.attachments.find((item: { label: string }) => item.label === 'synthetic.bin').gap, 'entry_not_provided');
      const tools = captures.filter(row => row.kind.startsWith('tool_'));
      assert.deepEqual(tools.map(row => row.kind).sort(), ['tool_call', 'tool_result']);
      assert.ok(tools.every(row => row.input_id === input.message.message_id && row.tool_name === 'synthetic-tool'));
      const object = db.prepare('SELECT content FROM attachments WHERE message_id=? AND attachment_id=?').get(input.message.message_id, 'native-0');
      assert.deepEqual(Buffer.from(object!.content as Uint8Array), data);
    } finally { db.close(); }
  } finally { await handle.dispose(); await host.close(); }
});
