import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { PassThrough } from 'node:stream';
import { mkdtemp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
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
import type { Config } from '../src/dsh.ts';
import { Tokenizer } from '@huggingface/tokenizers';
import { MemoryClient } from '../src/client.ts';

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
  toolName = 'synthetic-tool';
  toolArguments = '{"amount":37.50}';
  private toolUsed = false;
  interrupt = false;
  afterFirst?: () => Promise<void>;
  chunks?: StreamChunk[];
  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    this.requests.push(options);
    this.inspect?.(options);
    if (this.chunks) { yield* this.chunks; return; }
    if (this.toolOnce && !this.toolUsed) {
      this.toolUsed = true;
      const id = ToolCallId('synthetic-call');
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'tool-call-delta', index: 0, id, name: this.toolName, argumentsDelta: this.toolArguments };
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id, name: this.toolName, arguments: this.toolArguments } };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
      return;
    }
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: this.reply.slice(0, 3) };
    await this.afterFirst?.();
    if (this.interrupt) throw new Error('synthetic-provider-timeout');
    yield { type: 'text-delta', index: 0, text: this.reply.slice(3) };
    yield { type: 'block-end', index: 0, block: { type: 'text', text: this.reply } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

async function boot(adapter: Synthetic, budgeted = false, durable = false, lifecycle: { consolidation?: boolean } = {}) {
  await mkdir(output!, { recursive: true });
  const root = await mkdtemp(join(output!, 'native-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; config,access=initialize(Path(sys.argv[1]),client_id='synthetic-owner',entry='owner'); path=config.parent/'mcp-access-dsh.json'; pair_client(config,client_id='synthetic-dsh',entry='dsh',access_path=path,scopes=('capture','recall')); print(json.dumps({'config':str(config),'access':str(path)}))";
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
  const config: Config = { python: python!, config: paths.config, access: paths.access, entry: 'dsh', cwd: checkout };
  if (lifecycle.consolidation) {
    const access = join(root, 'memory/mcp-access-consolidation.json');
    execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime; from evernook.scheduler import ConsolidationScheduler; c=Path(sys.argv[1]); owner=c.parent/'mcp-access-owner.json'; a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=owner); pair_client(c,client_id='synthetic-consolidation',entry='dsh-background',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept')); ConsolidationScheduler(MemoryRuntime(c)).configure(a,paused=True,min_pending=1,idle_seconds=0,max_sources_per_run=4,max_runs_per_trigger=2,max_trigger_seconds=2)", paths.config, access], { cwd: checkout });
    config.consolidation = { access, pollMs: 100 };
  }
  if (durable) Object.assign(config, { streamMode: 'durable_text' });
  let target: Tokenizer | undefined;
  if (budgeted) {
    const tokenizer = { model: { type: 'WordLevel', vocab: { '[UNK]': 0, hello: 1, world: 2 }, unk_token: '[UNK]' },
      pre_tokenizer: { type: 'Whitespace' }, decoder: null, normalizer: null, post_processor: null, added_tokens: [] };
    const tokenizerText = JSON.stringify(tokenizer);
    const tokenizerFile = join(root, 'tokenizer.json');
    const tokenizerConfigFile = join(root, 'tokenizer_config.json');
    await writeFile(tokenizerFile, tokenizerText);
    await writeFile(tokenizerConfigFile, '{}');
    target = new Tokenizer(tokenizer, {});
    const counterFile = join(root, 'counter.json');
    await writeFile(counterFile, JSON.stringify({ schema_version: 1, node: process.execPath,
      package_dir: resolve(import.meta.dirname, '../node_modules/@huggingface/tokenizers'), model_id: 'local-test',
      tokenizer_file: tokenizerFile, config_file: tokenizerConfigFile,
      tokenizer_sha256: createHash('sha256').update(tokenizerText).digest('hex'),
      config_sha256: createHash('sha256').update('{}').digest('hex') }));
    config.contextConfig = join(root, 'context.json');
    await writeFile(config.contextConfig, JSON.stringify({ schema_version: 1, tokenizer_config: counterFile,
      policy: { normal_tokens: 512, deep_tokens: 1024, pinned_tokens: 128, recent_days: 7, recent_target_chars: 8192 } }));
    const counter = target;
    config.memoryBudget = { provider: 'synthetic', modelId: 'local-test', memoryTokens: 512,
      contextWindow: 20000, outputReserveTokens: 1000,
      countRequest: request => counter.encode(JSON.stringify({ messages: request.messages, system: request.system, tools: request.tools }), { add_special_tokens: false }).ids.length };
  }
  const memory = await installMemory(ctx, config);
  const errors: unknown[] = [];
  ctx.on('agent/error', ({ error }) => { errors.push(error); });
  function rows() {
    const db = new DatabaseSync(join(root, 'memory/memory.sqlite3'), { readOnly: true });
    try { return db.prepare('SELECT event,receipt FROM messages').all().map(row => ({ event: JSON.parse(String(row.event)), receipt: JSON.parse(String(row.receipt)) })); }
    finally { db.close(); }
  }
  return { root, ctx, memory, config, errors, rows, target,
    create: (session: string) => ctx.agents.create({ sessionId: SessionId(session), meta: { cwd: root, delegationDepth: 0 }, agentOptions: { provider: 'synthetic', model: 'local-test' } }),
    async close() { await memory.close(); await ctx.fiber.dispose(); },
  };
}
function user(id: string, text: string) { return freezeMessage({ id: MessageId(id), role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] }); }

test('native dsh lifecycle defers consolidation during provider work and resumes after the turn', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, false, true, { consolidation: true });
  t.after(() => host.close());
  const background = host.memory.consolidation;
  assert.ok(background);
  const handle = await host.create('consolidation-dsh');
  let checked = false;
  adapter.afterFirst = async () => {
    assert.equal((await background.status()).foreground_sessions, 1);
    execFileSync(python!, ['-m', 'evernook.cli', 'scheduler-configure', '--config', host.config.config,
      '--access', join(host.root, 'memory/mcp-access-owner.json')], { cwd: checkout, input: JSON.stringify({ paused: false }) });
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal((await background.status()).processed, 0);
    checked = true;
  };
  try {
    handle.agent.followup(user('consolidation-input', '合成：会话结束后有界处理'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    assert.equal(checked, true);
    const deadline = Date.now() + 20000;
    let status = await background.status();
    while ((status.pending !== 0 || status.foreground_sessions !== 0) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100)); status = await background.status();
    }
    assert.equal(status.pending, 0);
    assert.equal(status.foreground_sessions, 0);
    assert.ok(Number(status.covered_messages) > 0);
    assert.doesNotMatch(JSON.stringify(adapter.requests), /mcp-access-(consolidation|owner)/);
    await handle.dispose();
    const endedBy = Date.now() + 8000;
    status = await background.status();
    while (status.session_marks === 0 && Date.now() < endedBy) {
      await new Promise(resolve => setTimeout(resolve, 100)); status = await background.status();
    }
    assert.ok(Number(status.session_marks) > 0);
  } finally { await handle.dispose(); }
});

test('native dsh next-turn provider uses admitted preferences and excludes their suppressed profile and fact', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { consolidation: true });
  t.after(() => host.close());
  const handle = await host.create('automatic-preferences-dsh');
  t.after(() => handle.dispose());
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  function cli(command: string, input: Record<string, unknown> = {}) {
    return JSON.parse(execFileSync(python!, ['-m', 'evernook.cli', command, '--config', host.config.config, '--access', owner],
      { cwd: checkout, input: JSON.stringify(input), encoding: 'utf8' })).result;
  }
  const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
  Object.assign(saved.policy, { normal_tokens: 4096, deep_tokens: 8192, profile_tokens: 1000,
    fact_keys: [['user', 'preference:answer_verbosity']] });
  await writeFile(host.config.contextConfig!, JSON.stringify(saved));
  host.config.memoryBudget!.memoryTokens = 4096;
  const settings = cli('preference-update-status').settings;
  cli('preference-update-configure', { enabled: true, admitted_processor: settings.rules_processor });
  cli('scheduler-configure', { paused: false, max_trigger_seconds: 10 });
  handle.agent.followup(user('automatic-preference', '我偏好简短回答。'));
  await handle.agent.whenIdle();
  const deadline = Date.now() + 30000;
  let tags = cli('profile-summary').tags;
  while (!tags.length && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 200)); tags = cli('profile-summary').tags;
  }
  assert.equal(tags.length, 1);
  function payload() {
    const request = adapter.requests.at(-1)!;
    const blocks = request.messages.flatMap(message => message.content).filter(block => block.type === 'text')
      .filter(block => block.text.includes('untrusted_memory_context'));
    assert.equal(blocks.length, 1);
    return JSON.parse(blocks[0].text.slice(blocks[0].text.indexOf('{'))) as {
      items: { kind: string; data: { label?: string; fact?: { text: string } | null } }[];
      coverage: { lexical: { state: string } };
    };
  }
  handle.agent.followup(user('current-instruction', '合成：本轮请用表格，当前指令优先。'));
  await handle.agent.whenIdle();
  assert.ok(payload().items.some(item => item.kind === 'profile' && item.data.label === '我偏好简短回答。'));
  assert.ok(payload().items.some(item => item.kind === 'facts' && item.data.fact?.text === '我偏好简短回答。'));
  assert.equal(payload().coverage.lexical.state, 'not_built');
  assert.match(JSON.stringify(adapter.requests.at(-1)), /本轮请用表格/);
  handle.agent.followup(user('suppress-preference', '合成：确认抑制这个回答偏好画像。'));
  await handle.agent.whenIdle();
  cli('profile-decide', { request_id: 'suppress-automatic-preference', tag_id: tags[0].tag_id,
    expected_revision: tags[0].revision, action: 'suppress',
    confirmation_id: host.memory.status(handle.agent.session.id).latest_input!.message_id });
  handle.agent.followup(user('after-suppression', '合成：继续当前聊天。'));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  assert.equal(payload().items.some(item => item.kind === 'profile' && item.data.label === '我偏好简短回答。'), false);
  assert.equal(payload().items.some(item => item.kind === 'facts' && item.data.fact?.text === '我偏好简短回答。'), false);
  assert.doesNotMatch(JSON.stringify(adapter.requests), /mcp-access-(owner|consolidation)/);
});

test('native dsh cancellation and provider failure release foreground activity without disposing the session', async t => {
  for (const canceled of [true, false]) {
    const adapter = new Synthetic();
    const host = await boot(adapter, false, true, { consolidation: true });
    t.after(() => host.close());
    const background = host.memory.consolidation!;
    const handle = await host.create('consolidation-interrupted-' + canceled);
    t.after(() => handle.dispose());
    adapter.afterFirst = async () => { if (canceled) handle.agent.cancel({ kind: 'user' }); };
    adapter.interrupt = !canceled;
    handle.agent.followup(user('interrupted-input', '合成：中断不应永久阻塞后台'));
    await handle.agent.whenIdle();
    assert.equal(background.snapshot().foreground, 0);
    const releasedBy = Date.now() + 5000;
    let status = await background.status();
    while (status.foreground_sessions !== 0 && Date.now() < releasedBy) {
      await new Promise(resolve => setTimeout(resolve, 100)); status = await background.status();
    }
    assert.equal(status.foreground_sessions, 0);
    if (!canceled) assert.ok(host.errors.length > 0);
    await handle.dispose(); await host.close();
  }
});

test('durable text is published and independently replayable before the provider finishes', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter, false, true);
  const reader = new MemoryClient(host.config);
  await reader.connect();
  const displayed: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text);
  });
  const handle = await host.create('durable-live');
  let checked = false;
  adapter.afterFirst = async () => {
    assert.equal(displayed.join(''), '合成回');
    const found = await host.memory.streams(handle.agent.session.id);
    assert.equal(found.streams.length, 1);
    const page = await reader.readStream(found.streams[0]);
    assert.equal(page.text, '合成回');
    assert.equal(page.state, 'open');
    checked = true;
  };
  try {
    handle.agent.followup(user('durable-input', '合成：可靠分块'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    assert.equal(checked, true);
    assert.equal(displayed.join(''), adapter.reply);
    const found = await host.memory.streams(handle.agent.session.id);
    const page = await reader.readStream(found.streams[0]);
    assert.equal(page.text, adapter.reply);
    assert.equal(page.state, 'complete');
    assert.equal((await reader.read(page.final_message_id!)).event.text, adapter.reply);
  } finally { await reader.close(); await handle.dispose(); await host.close(); }
});

test('durable cancellation preserves the displayed prefix through a fresh adapter and repeated rescan', async () => {
  const adapter = new Synthetic();
  adapter.reply = '合成回password=synthetic-withheld-tail';
  const host = await boot(adapter, false, true);
  const displayed: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text);
  });
  const handle = await host.create('durable-cancel');
  adapter.afterFirst = async () => { handle.agent.cancel({ kind: 'user' }); };
  let fresh: Awaited<ReturnType<typeof installMemory>> | undefined;
  try {
    handle.agent.followup(user('cancel-input', '合成：取消可靠流'));
    await handle.agent.whenIdle();
    assert.equal(displayed.join(''), '合成回');
    await host.memory.close();
    fresh = await installMemory(host.ctx, host.config);
    await fresh.rescan(handle.agent.session.id);
    await fresh.rescan(handle.agent.session.id);
    const found = await fresh.streams(handle.agent.session.id);
    assert.equal(found.streams.length, 1);
    const page = await fresh.readStream(found.streams[0]);
    assert.equal(page.text, '合成回');
    assert.equal(page.state, 'canceled');
    assert.equal(page.complete, false);
    assert.equal(page.final_message_id, null);
    assert.deepEqual(await fresh.readStream(found.streams[0]), page);
  } finally { await fresh?.close(); await handle.dispose(); await host.close(); }
});

test('a killed real dsh process leaves displayed text discoverable without a native settlement', async () => {
  const host = await boot(new Synthetic(), false, true);
  await host.memory.close();
  const worker = childProcess.fork(join(import.meta.dirname, 'dsh-crash-worker.ts'), [JSON.stringify(host.config), host.root],
    { stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
  const exited = new Promise<void>(resolve => worker.once('exit', () => resolve()));
  let fresh: Awaited<ReturnType<typeof installMemory>> | undefined;
  try {
    const displayed = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('synthetic-worker-timeout')), 30000);
      worker.once('error', error => { clearTimeout(timer); reject(error); });
      worker.once('exit', () => { clearTimeout(timer); reject(new Error('synthetic-worker-exited')); });
      worker.once('message', value => {
        clearTimeout(timer);
        const message = value as { text?: string; error?: string };
        if (message.text) resolve(message.text); else reject(new Error(message.error));
      });
    });
    assert.equal(displayed, '已显示🙂');
    worker.kill('SIGKILL');
    await exited;
    fresh = await installMemory(host.ctx, host.config);
    await fresh.rescan(SessionId('durable-killed'));
    await fresh.rescan(SessionId('durable-killed'));
    const found = await fresh.streams(SessionId('durable-killed'));
    assert.equal(found.streams.length, 1);
    const reader = new MemoryClient(host.config);
    await reader.connect();
    try {
      const page = await reader.readStream(found.streams[0]);
      assert.equal(page.text, '已显示🙂');
      assert.equal(page.state, 'open');
      assert.equal(page.pending_chars, 4);
      assert.equal(page.complete, false);
      assert.deepEqual(await reader.readStream(found.streams[0]), page);
    } finally { await reader.close(); }
  } finally { worker.kill('SIGKILL'); await exited; await fresh?.close(); await host.close(); }
});

test('durable write failure after publication blocks the next delta and leaves its outcome reconcilable', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter, false, true);
  const displayed: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') displayed.push(frame.chunk.text);
  });
  adapter.afterFirst = async () => {
    const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'));
    try { db.exec("CREATE TRIGGER synthetic_chunk_failure BEFORE INSERT ON text_stream_chunks_v1 WHEN NEW.chunk_index >= 2 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"); }
    finally { db.close(); }
  };
  const handle = await host.create('durable-write-failure');
  try {
    handle.agent.followup(user('write-failure-input', '合成：中途保存失败'));
    await handle.agent.whenIdle();
    assert.equal(displayed.join(''), '合成回');
    assert.ok(host.errors.length > 0);
    const found = await host.memory.streams(handle.agent.session.id);
    const page = await host.memory.readStream(found.streams[0]);
    assert.equal(page.text, '合成回');
    assert.equal(page.state, 'open');
    assert.equal(page.next_index, 2);
  } finally { await handle.dispose(); await host.close(); }
});

test('durable mode screens split sensitive tails and rejects tool calls before publication or execution', async () => {
  for (const tool of [false, true]) {
    const adapter = new Synthetic();
    adapter.reply = 'password=synthetic-durable-output-sentinel';
    adapter.toolOnce = tool;
    const host = await boot(adapter, false, true);
    let executions = 0;
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic blocked tool',
      parameters: { amount: { type: 'number' } },
      execute: async () => { executions++; return [{ type: 'text', text: 'synthetic result' }]; } }));
    const visible: string[] = [];
    host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(JSON.stringify(frame.chunk)); });
    const handle = await host.create('durable-protected');
    try {
      handle.agent.followup(user('protected-input', '合成：受限路径'));
      await handle.agent.whenIdle();
      assert.ok(host.errors.length > 0);
      assert.equal(executions, 0);
      assert.doesNotMatch(visible.join(''), /password|synthetic-durable-output-sentinel|synthetic-call|amount/);
      assert.equal(host.memory.status(handle.agent.session.id).tool_publication, 'blocked');
      const found = await host.memory.streams(handle.agent.session.id);
      const page = await host.memory.readStream(found.streams[0]);
      assert.equal(page.text, '');
      assert.equal(page.state, tool ? 'failed' : 'protected');
      const paths = await readdir(join(host.root, 'memory'));
      const contents = Buffer.concat(await Promise.all(paths.filter(name => name.startsWith('memory.sqlite3')).map(name => readFile(join(host.root, 'memory', name)))));
      assert.ok(!contents.includes(Buffer.from('synthetic-durable-output-sentinel')));
    } finally { await handle.dispose(); await host.close(); }
  }
});

test('a truncated provider finish keeps the withheld tail incomplete rather than declaring a full answer', async () => {
  const adapter = new Synthetic();
  adapter.chunks = [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: '已显示pass' },
    { type: 'block-end', index: 0, block: { type: 'text', text: '已显示pass' } },
    { type: 'finish', reason: { kind: 'max-tokens' } },
  ];
  const host = await boot(adapter, false, true);
  const visible: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') visible.push(frame.chunk.text);
  });
  const handle = await host.create('durable-truncated');
  try {
    handle.agent.followup(user('truncated-input', '合成：输出上限'));
    await handle.agent.whenIdle();
    const found = await host.memory.streams(handle.agent.session.id);
    const page = await host.memory.readStream(found.streams[0]);
    assert.equal(page.state, 'failed');
    assert.equal(page.text, '已显示');
    assert.equal(page.pending_chars, 4);
    assert.equal(page.final_message_id, null);
    assert.equal(visible.join(''), '已显示');
    const reader = new MemoryClient(host.config);
    await reader.connect();
    try {
      const matches = await reader.call<{ hits: { role: string; completion: string }[] }>('search', { query: '已显示', mode: 'exact' });
      assert.ok(!matches.hits.some(hit => hit.role === 'assistant' && hit.completion === 'complete'));
    } finally { await reader.close(); }
  } finally { await handle.dispose(); await host.close(); }
});

test('budgeted durable text preserves split UTF-16 pairs and splits large provider deltas at the core byte limit', async () => {
  const adapter = new Synthetic();
  const text = '安全🙂' + '字'.repeat(22000) + '。';
  adapter.chunks = [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: '安全\uD83D' },
    { type: 'text-delta', index: 0, text: '\uDE42' + '字'.repeat(22000) + '。' },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ];
  const host = await boot(adapter, true, true);
  const visible: string[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') visible.push(frame.chunk.text);
  });
  const handle = await host.create('durable-unicode');
  try {
    handle.agent.followup(user('unicode-input', '合成：Unicode 分块'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    assert.equal(visible.join(''), text);
    const found = await host.memory.streams(handle.agent.session.id);
    let restored = '';
    let cursor = 0;
    for (;;) {
      const page = await host.memory.readStream(found.streams[0], cursor, 4096);
      restored += page.text;
      if (page.next_cursor === null) break;
      cursor = page.next_cursor;
    }
    assert.equal(restored, text);
  } finally { await handle.dispose(); await host.close(); }
});

test('a failed provider and a mismatching final block cannot publish uncommitted final text or diagnostics', async () => {
  for (const mismatch of [false, true]) {
    const adapter = new Synthetic();
    if (mismatch) adapter.chunks = [
      { type: 'block-start', index: 0, blockType: 'text' },
      { type: 'text-delta', index: 0, text: '已显示' },
      { type: 'block-end', index: 0, block: { type: 'text', text: 'password=synthetic-final-secret' } },
      { type: 'finish', reason: { kind: 'stop' } },
    ];
    else { adapter.reply = '已显示'; adapter.interrupt = true; }
    const host = await boot(adapter, false, true);
    const visible: string[] = [];
    host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(JSON.stringify(frame.chunk)); });
    const handle = await host.create('durable-provider-error');
    try {
      handle.agent.followup(user('provider-error-input', '合成：失败与最终块不一致'));
      await handle.agent.whenIdle();
      assert.ok(host.errors.length > 0);
      assert.doesNotMatch(visible.join(''), /synthetic-final-secret|password|synthetic-provider-timeout/);
      const found = await host.memory.streams(handle.agent.session.id);
      const page = await host.memory.readStream(found.streams[0]);
      assert.equal(page.text, '已显示');
      assert.equal(page.state, 'failed');
      assert.equal(page.final_message_id, null);
    } finally { await handle.dispose(); await host.close(); }
  }
});

test('target tokenizer budgets the real core payload and provider request with a least-privilege identity', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('token-budget');
  try {
    handle.agent.followup(user('budget-one', 'hello world'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    assert.equal(adapter.requests.length, 1);
    const request = adapter.requests[0];
    assert.equal(request.maxTokens, 1000);
    const blocks = request.messages.flatMap(message => message.content).filter(block => block.type === 'text');
    const memory = blocks.find(block => block.text.includes('untrusted_memory_context'));
    assert.ok(memory);
    assert.ok(host.target!.encode(memory.text, { add_special_tokens: false }).ids.length <= 512);
    assert.ok(host.config.memoryBudget!.countRequest(request) + 1000 <= 20000);
    assert.doesNotMatch(JSON.stringify(request.messages.filter(message => message.role === 'system')), /untrusted_memory_context/);
    assert.equal(host.memory.status(handle.agent.session.id).context_mode, 'target_tokenizer');
    assert.equal((await host.memory.settings()).core.saved_policy?.normal_tokens, 512);
    assert.equal((await host.memory.settings()).core.recommended_policy?.normal_tokens, 16000);
    const clients = JSON.parse(await readFile(join(host.root, 'memory/client.json'), 'utf8')) as { clients: { client_id: string; scopes: string[] }[] };
    assert.deepEqual(clients.clients.find(client => client.client_id === 'synthetic-dsh')?.scopes, ['capture', 'recall']);
    const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
    saved.policy.normal_tokens = 256;
    saved.policy.recent_days = 1;
    await writeFile(host.config.contextConfig!, JSON.stringify(saved));
    handle.agent.followup(user('budget-two', 'x'.repeat(257)));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    const state = host.memory.status(handle.agent.session.id).context;
    assert.equal(state?.token_limit, 256);
    assert.equal((await host.memory.settings()).core.saved_policy?.recent_days, 1);
    assert.ok(state?.degraded.includes('query_too_long'));
    assert.equal(state?.complete, false);
    const latest = adapter.requests.at(-1)!;
    const memories = latest.messages.flatMap(message => message.content).filter(block => block.type === 'text')
      .filter(block => block.text.includes('untrusted_memory_context'));
    assert.equal(memories.length, 1);
    assert.ok(memories.reduce((sum, block) => sum + host.target!.encode(block.text, { add_special_tokens: false }).ids.length, 0) <= 512);
  } finally { await handle.dispose(); await host.close(); }
});

test('budgeted native provider receives core persona profile facts and important words as data', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('core-components');
  try {
    const seed = [
      'import json,sys',
      'from pathlib import Path',
      'from evernook import Access,Event,Source,MemoryRuntime,EvidenceSpan,ImportantMark,CandidateDraft,CandidateWorkflow',
      'from evernook.persona import PersonaRegistry,PersonaSet',
      'from evernook.profile import ProfileRegistry,ProfileDecision,ProfileDraft',
      'from evernook.facts import FactRegistry,FactDecision',
      'runtime=MemoryRuntime(Path(sys.argv[1]))',
      "clients=json.loads((runtime.root/'client.json').read_text())['clients']",
      "owner=Access('synthetic-owner',next(c['token'] for c in clients if c['client_id']=='synthetic-owner'))",
      "def save(n,text): return runtime.submit(owner,Event(Source('owner','seed','main',str(n)),'user',n,None,'complete',text)).message_id",
      "confirmation=save(1,'synthetic persona confirmation')",
      "PersonaRegistry(runtime).set(owner,PersonaSet('persona-seed','tone','synthetic persona',confirmation,'synthetic',0))",
      "source=save(2,'hello synthetic interest')",
      "span=EvidenceSpan(source,1,'original',1,0,len('hello synthetic interest'),'hello synthetic interest')",
      "profiles=ProfileRegistry(runtime)",
      "profiles.decide(owner,ProfileDecision('profile-seed','interest',0,'propose',ProfileDraft('interest','synthetic interest',0.9,(span,))))",
      "profiles.decide(owner,ProfileDecision('profile-confirm','interest',1,'confirm',confirmation_id=save(3,'synthetic confirm interest')))",
      "important=runtime.mark_important(owner,ImportantMark(source,'original',1,'shared',None,None,'hello synthetic interest'))",
      "candidates=CandidateWorkflow(runtime)",
      "lease=candidates.begin(owner,(source,),processor_version='synthetic-native')",
      "candidates.complete(owner,lease.status.run_id,lease.token,(CandidateDraft('synthetic fact','user_statement',(span,)),))",
      "candidate=candidates.candidates(owner,lease.status.run_id)[0]",
      "FactRegistry(runtime).decide(owner,FactDecision('fact-seed','synthetic','preference',candidate.candidate_id,save(4,'synthetic confirm fact'),0,'assert','2026-01-01T00:00:00+00:00'))",
      "print(json.dumps({'important_id':important.important_id}))",
    ].join('\n');
    const seeded = JSON.parse(execFileSync(python!, ['-c', seed, host.config.config], { cwd: checkout, encoding: 'utf8' })) as { important_id: string };
    const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
    saved.policy = { normal_tokens: 4096, deep_tokens: 8192, pinned_tokens: 1000, recent_days: 7,
      recent_target_chars: 8192, persona_tokens: 1000, profile_tokens: 1000, pinned_ids: [seeded.important_id], fact_keys: [['synthetic', 'preference']] };
    await writeFile(host.config.contextConfig!, JSON.stringify(saved));
    host.config.memoryBudget!.memoryTokens = 4096;
    handle.agent.followup(user('components-input', 'hello'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    const request = adapter.requests[0];
    const memory = request.messages.flatMap(message => message.content).filter(block => block.type === 'text')
      .find(block => block.text.includes('untrusted_memory_context'));
    assert.ok(memory);
    for (const kind of ['persona', 'profile', 'facts', 'important']) assert.match(memory.text, new RegExp(`"kind":"${kind}"`));
    assert.ok(host.target!.encode(memory.text, { add_special_tokens: false }).ids.length <= 4096);
  } finally { await handle.dispose(); await host.close(); }
});

test('invalid saved budget and exhausted host window block dispatch before the provider', async () => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('window-full');
  try {
    host.config.memoryBudget!.countRequest = () => 20000;
    handle.agent.followup(user('window-input', 'hello'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 0);
    assert.ok(host.errors.some(error => String(error).includes('host_context_window_exceeded')));
    const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
    saved.policy.normal_tokens = true;
    await writeFile(host.config.contextConfig!, JSON.stringify(saved));
    handle.agent.followup(user('invalid-budget-input', 'hello world'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 0);
    assert.ok(host.errors.some(error => String(error).includes('invalid_context_policy')));
  } finally { await handle.dispose(); await host.close(); }
});

test('request counting matches the actual SDK file and tool projection received by the provider', async () => {
  const adapter = new Synthetic();
  adapter.toolOnce = true;
  const host = await boot(adapter, true);
  host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic local fixture',
    parameters: { amount: { type: 'number' } }, execute: async () => [{ type: 'text', text: 'synthetic result' }] }));
  const handle = await host.create('budget-files-tools');
  try {
    const actual = host.config.memoryBudget!.countRequest;
    let checked = 0;
    host.config.memoryBudget!.countRequest = request => { checked = actual(request); return checked; };
    adapter.inspect = request => assert.equal(actual(request), checked);
    const file = await host.ctx.attachments.saveFile({ data: Buffer.from('hello world'), name: 'synthetic.txt' });
    handle.agent.followup(freezeMessage({ id: MessageId('budget-file'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: 'hello' }, { type: 'file', attachment: file }] }));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []);
    assert.equal(adapter.requests.length, 2);
    assert.ok(adapter.requests.every(request => !request.messages.flatMap(message => message.content).some(block => block.type === 'file')));
  } finally { await handle.dispose(); await host.close(); }
});

test('native user confirmation pins exact long words immediately and pages them through ordinary recall', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('confirmed-pin');
  const ordinary = new MemoryClient(host.config);
  await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const previews: unknown[] = [];
  const receipts: unknown[] = [];
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, {
    access, onReview: async review => { previews.push(review); }, onResult: async result => { receipts.push(result); } });
  try {
    const words = '合成37.50元🙂\r\ne\u0301'.repeat(400);
    handle.agent.followup(user('long-words', words));
    await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id;
    assert.ok(id);
    const review = await maintenance.propose(handle.agent, { kind: 'pin', message_id: id });
    assert.equal(previews.length, 1);
    const before = await ordinary.call<{ ledger: { important_items: number } }>('status', {});
    assert.equal(before.ledger.important_items, 0);
    adapter.reply = review.confirm_command;
    handle.agent.followup(user('not-confirmed', '合成：先别执行。'));
    await handle.agent.whenIdle();
    assert.equal((await ordinary.call<{ ledger: { important_items: number } }>('status', {})).ledger.important_items, 0);
    handle.agent.followup(user('confirm-pin', review.confirm_command));
    await handle.agent.whenIdle();
    const outcome = maintenance.status(review.proposal_id);
    assert.equal(outcome?.state, 'completed');
    const importantId = outcome?.result?.important_id;
    assert.ok(typeof importantId === 'string');
    let cursor = 0;
    let restored = '';
    for (;;) {
      const page = await maintenance.readImportant(importantId, cursor, 512);
      restored += page.text;
      if (page.next_cursor === null) break;
      cursor = page.next_cursor;
    }
    assert.equal(restored, words);
    assert.equal(receipts.length, 1);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('native confirmation suppresses a source while the ordinary model identity remains unable to maintain it', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const host = await boot(new Synthetic(), true);
  const handle = await host.create('confirmed-suppression');
  const ordinary = new MemoryClient(host.config);
  await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access,
    onReview: async () => {}, onResult: async () => {} });
  try {
    handle.agent.followup(user('suppress-source', 'hello synthetic old'));
    await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id;
    assert.ok(id);
    const review = await maintenance.propose(handle.agent, { kind: 'suppress', message_id: id });
    assert.equal((await ordinary.read(id)).event.text, 'hello synthetic old');
    handle.agent.followup(user('confirm-suppress', review.confirm_command));
    await handle.agent.whenIdle();
    assert.equal(maintenance.status(review.proposal_id)?.state, 'completed');
    await assert.rejects(ordinary.read(id), /message_not_found/);
    assert.equal((await ordinary.call<{ status: string }>('state', { message_id: id })).status, 'suppressed');
    const confirmation = host.memory.status(handle.agent.session.id).latest_input?.message_id;
    await assert.rejects(ordinary.call('change', { change: { request_id: 'model-cannot-maintain', target_id: id,
      expected_version: 2, action: 'revoke', evidence_id: confirmation, reason: 'synthetic forbidden' } }), /permission_denied/);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('native purge needs an exact preview and later user confirmation and reports retained external copies', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const host = await boot(new Synthetic(), true);
  const handle = await host.create('confirmed-purge');
  const ordinary = new MemoryClient(host.config);
  await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access,
    onReview: async () => {}, onResult: async () => {} });
  try {
    handle.agent.followup(user('delete-source', 'hello synthetic delete'));
    await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id;
    assert.ok(id);
    const original = await ordinary.read(id);
    const review = await maintenance.propose(handle.agent, { kind: 'purge', source_ids: [id] });
    assert.deepEqual(review.residuals, ['existing_archives_and_backups_retained', 'host_copies_not_controlled']);
    assert.equal((await ordinary.read(id)).event.text, 'hello synthetic delete');
    handle.agent.followup(user('confirm-purge', review.confirm_command));
    await handle.agent.whenIdle();
    const outcome = maintenance.status(review.proposal_id);
    assert.equal(outcome?.state, 'completed');
    assert.equal(outcome?.result?.state, 'completed');
    assert.deepEqual(outcome?.result?.residuals, review.residuals);
    await assert.rejects(ordinary.read(id), /message_not_found/);
    await assert.rejects(ordinary.remember(original.event), /source_purged/);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('a reviewed operation remains bound to its original runtime when host configuration objects change', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const host = await boot(new Synthetic(), true);
  const handle = await host.create('maintenance-route');
  const ordinary = new MemoryClient(host.config);
  await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const options = { access, onReview: async () => {}, onResult: async () => {} };
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, options);
  try {
    handle.agent.followup(user('route-source', 'synthetic route evidence'));
    await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id;
    assert.ok(id);
    const review = await maintenance.propose(handle.agent, { kind: 'pin', message_id: id });
    const clone = join(host.root, 'clone');
    const cloneScript = [
      'import json,shutil,sqlite3,sys', 'from pathlib import Path', 'from evernook import initialize',
      'source=Path(sys.argv[1]).parent; target=Path(sys.argv[2])',
      "initialize(target,client_id='seed',entry='seed')",
      "a=sqlite3.connect(source/'memory.sqlite3'); b=sqlite3.connect(target/'memory.sqlite3'); a.backup(b); b.close(); a.close()",
      "for name in ('idempotency.key','client.json','mcp-access-maintenance.json'): shutil.copyfile(source/name,target/name)",
    ].join('\n');
    execFileSync(python!, ['-c', cloneScript, host.config.config, clone], { cwd: checkout });
    host.config.config = join(clone, 'runtime.json');
    options.access = join(clone, 'mcp-access-maintenance.json');
    handle.agent.followup(user('route-confirm', review.confirm_command));
    await handle.agent.whenIdle();
    assert.equal((await ordinary.call<{ ledger: { important_items: number } }>('status', {})).ledger.important_items, 1);
    const clonedStatus = JSON.parse(execFileSync(python!, ['-m', 'evernook.cli', 'status', '--config', host.config.config,
      '--access', options.access], { cwd: checkout, encoding: 'utf8' })) as { result: { ledger: { important_items: number } } };
    assert.equal(clonedStatus.result.ledger.important_items, 0);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('native profile revision confirmation and suppression use current core versions in the next context', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('profile-maintenance');
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access, onReview: async () => {}, onResult: async () => {} });
  const profile = () => JSON.parse(execFileSync(python!, ['-m', 'evernook.cli', 'profile-read', '--config', host.config.config,
    '--access', access], { cwd: checkout, input: JSON.stringify({ tag_id: 'native-interest' }), encoding: 'utf8' })).result;
  const labels = () => {
    const memory = adapter.requests.at(-1)?.messages.flatMap(message => message.content).filter(block => block.type === 'text')
      .find(block => block.text.includes('untrusted_memory_context'));
    assert.ok(memory);
    const payload: { items: { kind: string; data: { label?: string } }[] } = JSON.parse(memory.text.slice(memory.text.indexOf('{')));
    return payload.items.filter(item => item.kind === 'profile').map(item => item.data.label);
  };
  try {
    const policy = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
    policy.policy.normal_tokens = 2048; policy.policy.deep_tokens = 4096; policy.policy.profile_tokens = 1000;
    await writeFile(host.config.contextConfig!, JSON.stringify(policy)); host.config.memoryBudget!.memoryTokens = 2048;
    const seed = [
      'import json,sys', 'from pathlib import Path', 'from evernook import Access,MemoryRuntime,Event,Source,EvidenceSpan',
      'from evernook.profile import ProfileRegistry,ProfileDraft,ProfileDecision',
      'r=MemoryRuntime(Path(sys.argv[1]))', "clients=json.loads((r.root/'client.json').read_text())['clients']",
      "a=Access('synthetic-owner',next(c['token'] for c in clients if c['client_id']=='synthetic-owner'))",
      "s=r.submit(a,Event(Source('owner','profile','main','1'),'user',1,None,'complete','hello old preference'))",
      "p=ProfileRegistry(r); span=EvidenceSpan(s.message_id,1,'original',1,0,len('hello old preference'),'hello old preference')",
      "p.decide(a,ProfileDecision('native-profile-propose','native-interest',0,'propose',ProfileDraft('interest','old preference',0.8,(span,))))",
      "c=r.submit(a,Event(Source('owner','profile','main','2'),'user',2,None,'complete','synthetic confirm profile'))",
      "p.decide(a,ProfileDecision('native-profile-confirm','native-interest',1,'confirm',confirmation_id=c.message_id))",
    ].join('\n');
    execFileSync(python!, ['-c', seed, host.config.config], { cwd: checkout });
    handle.agent.followup(user('profile-new-source', 'hello new preference')); await handle.agent.whenIdle();
    const source = host.memory.status(handle.agent.session.id).latest_input?.message_id; assert.ok(source);
    const revised = await maintenance.propose(handle.agent, { kind: 'profile', tag_id: 'native-interest', action: 'revise',
      draft: { category: 'interest', label: 'new preference', confidence: 0.9, sources: [{ message_id: source,
        state_version: 1, representation: 'original', content_version: 1, start: 0, end: 20, expected_text: 'hello new preference' }] } });
    handle.agent.followup(user('profile-revise-confirm', revised.confirm_command)); await handle.agent.whenIdle();
    assert.equal(profile().state, 'draft');
    const confirmed = await maintenance.propose(handle.agent, { kind: 'profile', tag_id: 'native-interest', action: 'confirm' });
    handle.agent.followup(user('profile-confirm', confirmed.confirm_command)); await handle.agent.whenIdle();
    assert.equal(profile().state, 'confirmed');
    assert.ok(labels().includes('new preference'));
    const suppressed = await maintenance.propose(handle.agent, { kind: 'profile', tag_id: 'native-interest', action: 'suppress' });
    handle.agent.followup(user('profile-suppress-confirm', suppressed.confirm_command)); await handle.agent.whenIdle();
    assert.equal(profile().state, 'suppressed');
    assert.ok(!labels().includes('new preference'));
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await handle.dispose(); await host.close(); }
});

test('native fact correction preserves time and later source suppression cannot revive its older value', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('fact-maintenance');
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access, onReview: async () => {}, onResult: async () => {} });
  const fact = () => JSON.parse(execFileSync(python!, ['-m', 'evernook.cli', 'fact-query', '--config', host.config.config,
    '--access', access], { cwd: checkout, input: JSON.stringify({ subject_id: 'synthetic-user', predicate: 'home' }), encoding: 'utf8' })).result;
  try {
    const seed = [
      'import json,sys', 'from pathlib import Path', 'from evernook import Access,MemoryRuntime,Event,Source,EvidenceSpan,CandidateDraft,CandidateWorkflow',
      'from evernook.facts import FactRegistry,FactDecision',
      'r=MemoryRuntime(Path(sys.argv[1]))', "clients=json.loads((r.root/'client.json').read_text())['clients']",
      "a=Access('synthetic-owner',next(c['token'] for c in clients if c['client_id']=='synthetic-owner'))",
      "def save(n,text): return r.submit(a,Event(Source('owner','fact','main',str(n)),'user',n,None,'complete',text)).message_id",
      'w=CandidateWorkflow(r)',
      'def candidate(n,text):',
      ' s=save(n,text); lease=w.begin(a,(s,),processor_version="native-fact-test")',
      ' w.complete(a,lease.status.run_id,lease.token,(CandidateDraft(text,"user_statement",(EvidenceSpan(s,1,"original",1,0,len(text),text),)),))',
      ' return s,w.candidates(a,lease.status.run_id)[0]',
      "old_source,old=candidate(1,'hello old city')",
      "FactRegistry(r).decide(a,FactDecision('old-fact','synthetic-user','home',old.candidate_id,save(2,'synthetic confirm old city'),0,'assert','2026-01-01T00:00:00+00:00'))",
      "new_source,new=candidate(3,'hello new city')",
      "print(json.dumps({'source_id':new_source,'candidate_id':new.candidate_id,'run_id':new.run_id}))",
    ].join('\n');
    const seeded = JSON.parse(execFileSync(python!, ['-c', seed, host.config.config], { cwd: checkout, encoding: 'utf8' }));
    const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
    saved.policy.normal_tokens = 4096; saved.policy.deep_tokens = 8192; saved.policy.fact_keys = [['synthetic-user', 'home']];
    await writeFile(host.config.contextConfig!, JSON.stringify(saved)); host.config.memoryBudget!.memoryTokens = 4096;
    const review = await maintenance.propose(handle.agent, { kind: 'fact_correction', subject_id: 'synthetic-user', predicate: 'home',
      candidate_id: seeded.candidate_id, run_id: seeded.run_id });
    assert.equal(fact().fact.text, 'hello old city');
    handle.agent.followup(user('fact-correct-confirm', review.confirm_command)); await handle.agent.whenIdle();
    assert.equal(maintenance.status(review.proposal_id)?.state, 'completed');
    assert.equal(fact().fact.text, 'hello new city');
    assert.equal(fact().fact.valid_from, '2026-01-01T00:00:00+00:00');
    const suppress = await maintenance.propose(handle.agent, { kind: 'suppress', message_id: seeded.source_id });
    handle.agent.followup(user('fact-source-suppress-confirm', suppress.confirm_command)); await handle.agent.whenIdle();
    assert.equal(fact().state, 'source_unavailable');
    assert.equal(fact().fact, null);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await handle.dispose(); await host.close(); }
});

test('the actual native model tool can only propose and the next native user must confirm', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const adapter = new Synthetic();
  const host = await boot(adapter, true);
  const handle = await host.create('model-proposal-only');
  const ordinary = new MemoryClient(host.config); await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  let preview: { proposal_id: string; confirm_command: string } | undefined;
  let executed = 0;
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access,
    onReview: async review => { preview = review; }, onResult: async () => { executed++; } });
  try {
    handle.agent.followup(user('tool-source', 'hello user evidence')); await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id; assert.ok(id);
    adapter.toolOnce = true; adapter.toolName = 'evernook_memory_propose';
    adapter.toolArguments = JSON.stringify({ proposal: JSON.stringify({ kind: 'suppress', message_id: id }) });
    handle.agent.followup(user('request-proposal', '合成：请提出抑制建议，等待我的确认。')); await handle.agent.whenIdle();
    assert.ok(preview);
    assert.equal(executed, 0);
    assert.equal((await ordinary.read(id)).event.text, 'hello user evidence');
    handle.agent.followup(user('tool-confirm', preview.confirm_command)); await handle.agent.whenIdle();
    assert.equal(executed, 1);
    await assert.rejects(ordinary.read(id), /message_not_found/);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('native source correction uses the reviewed user replacement rather than the confirmation command as text', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const host = await boot(new Synthetic(), true);
  const handle = await host.create('source-correction');
  const ordinary = new MemoryClient(host.config); await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access, onReview: async () => {}, onResult: async () => {} });
  try {
    handle.agent.followup(user('old-source', 'hello old value')); await handle.agent.whenIdle();
    const target = host.memory.status(handle.agent.session.id).latest_input?.message_id; assert.ok(target);
    handle.agent.followup(user('replacement-source', 'hello corrected value')); await handle.agent.whenIdle();
    const replacement = host.memory.status(handle.agent.session.id).latest_input?.message_id; assert.ok(replacement);
    const review = await maintenance.propose(handle.agent, { kind: 'correct_source', message_id: target, replacement_id: replacement });
    handle.agent.followup(user('source-correct-confirm', review.confirm_command)); await handle.agent.whenIdle();
    assert.equal(maintenance.status(review.proposal_id)?.state, 'completed');
    const state = await ordinary.call<{ status: string; replacement_id: string }>('state', { message_id: target });
    assert.equal(state.status, 'corrected'); assert.equal(state.replacement_id, replacement);
    assert.equal((await ordinary.read(replacement)).event.text, 'hello corrected value');
    await assert.rejects(ordinary.read(target), /message_not_found/);
    assert.deepEqual(host.errors, []);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('a failed trusted review cannot authorize maintenance or expose renderer diagnostics to the model', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const host = await boot(new Synthetic(), true);
  const handle = await host.create('failed-review');
  const ordinary = new MemoryClient(host.config); await ordinary.connect();
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  let reviewId = '';
  const canceled = new AbortController();
  let rejectReview = true;
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access,
    onReview: async review => { reviewId = review.proposal_id;
      if (rejectReview) throw new Error('synthetic private renderer diagnostic');
      canceled.abort();
    }, onResult: async () => {} });
  try {
    handle.agent.followup(user('failed-review-source', 'hello exact words')); await handle.agent.whenIdle();
    const id = host.memory.status(handle.agent.session.id).latest_input?.message_id; assert.ok(id);
    await assert.rejects(maintenance.propose(handle.agent, { kind: 'pin', message_id: id }), /evernook:maintenance_review_unavailable/);
    handle.agent.followup(user('failed-review-confirm', '/memory confirm ' + reviewId)); await handle.agent.whenIdle();
    assert.equal((await ordinary.call<{ ledger: { important_items: number } }>('status', {})).ledger.important_items, 0);
    assert.equal(maintenance.status(reviewId), undefined);
    rejectReview = false;
    await assert.rejects(maintenance.propose(handle.agent, { kind: 'pin', message_id: id }, canceled.signal));
    handle.agent.followup(user('canceled-review-confirm', '/memory confirm ' + reviewId)); await handle.agent.whenIdle();
    assert.equal(maintenance.status(reviewId), undefined);
    assert.equal((await ordinary.call<{ ledger: { important_items: number } }>('status', {})).ledger.important_items, 0);
  } finally { await maintenance.close(); await ordinary.close(); await handle.dispose(); await host.close(); }
});

test('a lost CLI receipt retries the same committed change only after another actual user confirmation', async () => {
  const { installDshMaintenance } = await import('../src/dsh-maintenance.ts');
  const adapter = new Synthetic();
  const host = await boot(adapter);
  const handle = await host.create('maintenance-lost-receipt');
  const access = join(host.root, 'memory/mcp-access-maintenance.json');
  execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='dsh-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", host.config.config, access], { cwd: checkout });
  const maintenance = await installDshMaintenance(host.ctx, host.config, host.memory, { access,
    onReview: async () => {}, onResult: async () => {} });
  const spawn = childProcess.spawn;
  let dropReceipt = true;
  try {
    handle.agent.followup(user('lost-receipt-source', '合成：原文需要抑制'));
    await handle.agent.whenIdle();
    const id = host.rows().find(row => row.event.text === '合成：原文需要抑制')!.receipt.message_id;
    const review = await maintenance.propose(handle.agent, { kind: 'suppress', message_id: id });
    // Keep the real child and committed database; lose only the first CLI response bytes.
    childProcess.spawn = ((...args: Parameters<typeof spawn>) => {
      const child = spawn(...args);
      if (dropReceipt && Array.isArray(args[1]) && args[1].includes('change')) {
        dropReceipt = false;
        child.stdout?.resume();
        child.stdout = new PassThrough();
      }
      return child;
    }) as typeof spawn;
    syncBuiltinESMExports();
    handle.agent.followup(user('lost-receipt-confirm-1', review.confirm_command));
    await handle.agent.whenIdle();
    assert.equal(dropReceipt, false);
    assert.equal(maintenance.status(review.proposal_id)?.state, 'failed');
    const confirmation = maintenance.status(review.proposal_id)?.confirmation_id;
    const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'), { readOnly: true });
    try {
      assert.equal(db.prepare('SELECT count(*) AS n FROM changes WHERE target_id=?').get(id)?.n, 1);
      handle.agent.followup(user('lost-receipt-confirm-2', review.confirm_command));
      await handle.agent.whenIdle();
      assert.equal(maintenance.status(review.proposal_id)?.state, 'completed');
      assert.equal(maintenance.status(review.proposal_id)?.confirmation_id, confirmation);
      assert.equal(db.prepare('SELECT count(*) AS n FROM changes WHERE target_id=?').get(id)?.n, 1);
      const result = maintenance.status(review.proposal_id)?.result;
      assert.equal(result?.status, 'suppressed');
      assert.equal(result?.version, 2);
    } finally { db.close(); }
  } finally {
    childProcess.spawn = spawn; syncBuiltinESMExports();
    await maintenance.close(); await handle.dispose(); await host.close();
  }
});

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
