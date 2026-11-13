import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { PassThrough } from 'node:stream';
import { mkdtemp, mkdir, readFile, readdir, writeFile, rename } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import LlmRuntime, { LlmAdapter, MessageId, ToolCallId, freezeMessage, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId, SessionLogOffset, buildForkSeed, pluginRecordOf } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime, { defineContentToolFixture } from '@deepseek-ai/dsh-tools';
import TokenMeter from '@deepseek-ai/dsh-token-meter';
import BasicCompaction, { type BasicCompactionConfig } from '@deepseek-ai/dsh-compaction-basic';
import type { CompactionResult } from '@deepseek-ai/dsh-compaction';
import { installMemory as installMemorySource } from '../src/dsh.ts';
import type { Config } from '../src/dsh.ts';
import { createRecovery } from '../src/dsh-recovery.ts';
import { runLocalCli } from '../src/local-cli.ts';
import { Tokenizer } from '@huggingface/tokenizers';
import { MemoryClient } from '../src/client.ts';
import { installWeb as installWebSource } from '../src/dsh-web.ts';
import { pathToFileURL } from 'node:url';

const installWeb: typeof installWebSource = process.env.EVERNOOK_TEST_WEB_MODULE
  ? (await import(pathToFileURL(process.env.EVERNOOK_TEST_WEB_MODULE).href)).installWeb
  : installWebSource;
const installMemory: typeof installMemorySource = process.env.EVERNOOK_TEST_NATIVE_MODULE
  ? (await import(pathToFileURL(process.env.EVERNOOK_TEST_NATIVE_MODULE).href)).installMemory
  : installMemorySource;

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
  afterChunk?: (index: number) => Promise<void>;
  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    this.requests.push(options);
    this.inspect?.(options);
    if (this.chunks) {
      let index = 0;
      for (const chunk of this.chunks) { yield chunk; await this.afterChunk?.(index++); }
      return;
    }
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

async function boot(adapter: Synthetic, budgeted = false, durable = false, lifecycle: { service?: boolean; reflectionConfig?: string; reflectionAllowRemote?: boolean; consolidation?: boolean; compaction?: boolean | BasicCompactionConfig; toolPublication?: boolean; streamMode?: Config['streamMode']; maxParallelToolCalls?: number } = {}) {
  await mkdir(output!, { recursive: true });
  const root = await mkdtemp(join(output!, 'native-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; config,access=initialize(Path(sys.argv[1]),client_id='synthetic-owner',entry='owner'); owner=config.parent/'mcp-access-owner.json'; pair_client(config,client_id='synthetic-owner',entry='owner',access_path=owner); path=config.parent/'mcp-access-dsh.json'; pair_client(config,client_id='synthetic-dsh',entry='dsh',access_path=path,scopes=('capture','recall')); print(json.dumps({'config':str(config),'access':str(path)}))";
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
  await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: lifecycle.maxParallelToolCalls ?? 1 }).await();
  if (lifecycle.compaction) {
    await ctx.plugin(TokenMeter).await();
    await ctx.plugin(BasicCompaction, { auto: false, maxTokens: 512, headroomTokens: 512,
      ...(typeof lifecycle.compaction === 'object' ? lifecycle.compaction : {}) }).await();
  }
  ctx.llm.registerAdapter(['synthetic'], adapter);
  const config: Config = { python: python!, config: paths.config, access: paths.access, entry: 'dsh', cwd: checkout };
  if (lifecycle.reflectionConfig) config.reflection = true;
  if (lifecycle.reflectionAllowRemote) config.reflectionAllowRemote = true;
  if (lifecycle.consolidation) {
    const access = join(root, 'memory/mcp-access-consolidation.json');
    execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime; from evernook.scheduler import ConsolidationScheduler; c=Path(sys.argv[1]); owner=c.parent/'mcp-access-owner.json'; a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=owner); pair_client(c,client_id='synthetic-consolidation',entry='dsh-background',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept')); ConsolidationScheduler(MemoryRuntime(c)).configure(a,paused=True,min_pending=1,idle_seconds=0,max_sources_per_run=4,max_runs_per_trigger=2,max_trigger_seconds=2)", paths.config, access], { cwd: checkout });
    config.consolidation = { access, pollMs: 100 };
  }
  if (durable) Object.assign(config, { streamMode: 'durable_text' });
  if (lifecycle.toolPublication) Object.assign(config, { toolPublication: true });
  if (lifecycle.streamMode) Object.assign(config, { streamMode: lifecycle.streamMode });
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
    if (lifecycle.compaction) config.memoryBudget.memoryTokens = 2048;
  }
  let service: childProcess.ChildProcess | undefined;
  if (lifecycle.service) {
    config.serviceRequired = true;
    const args = ['-B', '-m', 'evernook.service', '--config', config.config, '--activate-owner', join(root, 'memory/mcp-access-owner.json')];
    if (config.contextConfig) args.push('--context-config', config.contextConfig);
    if (lifecycle.reflectionConfig) args.push('--reflection-config', lifecycle.reflectionConfig);
    service = childProcess.spawn(python!, args, { cwd: checkout, windowsHide: true,
      stdio: ['ignore', 'ignore', 'ignore'] });
    const deadline = Date.now() + 15000;
    while (true) {
      try { await readFile(join(root, 'memory/memory-service.json')); break; }
      catch {
        if (service.exitCode !== null || Date.now() > deadline) throw new Error('service readiness failed');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }
  }
  let memory: Awaited<ReturnType<typeof installMemory>>;
  try { memory = await installMemory(ctx, config); }
  catch (error) {
    try { await ctx.fiber.dispose(); }
    finally {
      if (service && service.exitCode === null) {
        const stopped = new Promise<void>(resolve => service!.once('exit', () => resolve()));
        service.kill(); await stopped;
      }
    }
    throw error;
  }
  const errors: unknown[] = [];
  ctx.on('agent/error', ({ error }) => { errors.push(error); });
  function rows() {
    const db = new DatabaseSync(join(root, 'memory/memory.sqlite3'), { readOnly: true });
    try { return db.prepare('SELECT event,receipt FROM messages').all().map(row => ({ event: JSON.parse(String(row.event)), receipt: JSON.parse(String(row.receipt)) })); }
    finally { db.close(); }
  }
  return { root, ctx, memory, config, errors, rows, target,
    create: (session: string) => ctx.agents.create({ sessionId: SessionId(session), meta: { cwd: root, delegationDepth: 0 }, agentOptions: { provider: 'synthetic', model: 'local-test' } }),
    async close() {
      try { await memory.close(); await ctx.fiber.dispose(); }
      finally {
        if (service && service.exitCode === null) {
          const stopped = new Promise<void>(resolve => service!.once('exit', () => resolve()));
          service.kill(); await stopped;
        }
      }
    },
  };
}
function user(id: string, text: string) { return freezeMessage({ id: MessageId(id), role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] }); }

test('explicit local reflection tool returns verified original evidence to the actual dsh provider',
    { skip: !process.env.EVERNOOK_TEST_REFLECTION_CONFIG, timeout: 120000 }, async t => {
  const adapter = new Synthetic(); adapter.toolOnce = true; adapter.toolName = 'evernook_memory_reflect';
  adapter.toolArguments = JSON.stringify({ query: '旧相册' });
  const patched = Reflect.get(ToolRuntime, 'evernookPublicationVersion') === 1;
  const host = await boot(adapter, false, patched, { service: true, reflectionConfig: process.env.EVERNOOK_TEST_REFLECTION_CONFIG,
    streamMode: patched ? 'durable_tools' : 'buffered', toolPublication: patched });
  t.after(() => host.close());
  const seed = "import sys,json; from pathlib import Path; from dataclasses import asdict; from evernook import Event,Source; from evernook.service import ServiceClient,client_access; c=Path(sys.argv[1]); a=client_access(c,c.parent/'mcp-access-owner.json'); s=ServiceClient(c); r=s.call(a,'remember',{'event':asdict(Event(Source('owner','old','main','1'),'user',1,'2020-01-01T00:00:00Z','complete','合成：旧相册记录了银叶桥湿地的苍鹭。'))}); s.call(a,'cli',{'command':'reflect-configure','input':{}}); print(json.dumps(r))";
  const receipt = JSON.parse(execFileSync(python!, ['-B', '-c', seed, host.config.config], { cwd: checkout, encoding: 'utf8' })) as { message_id: string };
  const handle = await host.create('explicit-reflection'); t.after(() => handle.dispose());
  handle.agent.followup(user('reflect-input', '请深入追忆旧相册里的共同经历。')); await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  assert.ok(adapter.requests.length >= 2);
  const finalRequest = JSON.stringify(adapter.requests.at(-1));
  assert.ok(finalRequest.includes('untrusted_memory_reflection'));
  assert.ok(finalRequest.includes(receipt.message_id));
  assert.ok(finalRequest.includes('旧相册记录了银叶桥湿地的苍鹭'));
  assert.ok(finalRequest.includes('temporary_only'));
});

test('explicit remote reflection uses trusted host consent and returns verified evidence to dsh',
    { skip: !process.env.EVERNOOK_TEST_REMOTE_REFLECTION_CONFIG, timeout: 120000 }, async t => {
  const adapter = new Synthetic(); adapter.toolOnce = true; adapter.toolName = 'evernook_memory_reflect';
  adapter.toolArguments = JSON.stringify({ query: '旧相册' });
  const patched = Reflect.get(ToolRuntime, 'evernookPublicationVersion') === 1;
  const host = await boot(adapter, false, patched, { service: true,
    reflectionConfig: process.env.EVERNOOK_TEST_REMOTE_REFLECTION_CONFIG, reflectionAllowRemote: true,
    streamMode: patched ? 'durable_tools' : 'buffered', toolPublication: patched });
  t.after(() => host.close());
  const seed = "import sys; from pathlib import Path; from dataclasses import asdict; from evernook import Event,Source; from evernook.service import ServiceClient,client_access; c=Path(sys.argv[1]); a=client_access(c,c.parent/'mcp-access-owner.json'); s=ServiceClient(c); s.call(a,'remember',{'event':asdict(Event(Source('owner','old','main','1'),'user',1,None,'complete','合成：旧相册记录了银叶桥湿地的苍鹭。'))}); s.call(a,'cli',{'command':'reflect-configure','input':{}})";
  execFileSync(python!, ['-B', '-c', seed, host.config.config], { cwd: checkout });
  const handle = await host.create('explicit-remote-reflection'); t.after(() => handle.dispose());
  handle.agent.followup(user('remote-reflect-input', '请深入追忆旧相册。')); await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  const request = JSON.stringify(adapter.requests.at(-1));
  assert.ok(request.includes('untrusted_memory_reflection'));
  assert.ok(request.replaceAll('\\"', '"').includes('"provider":"remote"'));
  assert.ok(request.includes('旧相册记录了银叶桥湿地的苍鹭'));
  assert.ok(request.includes('cost_reserved_micro_usd'));
  assert.ok(request.includes('temporary_only'));
});

for (const budgeted of [false, true]) test(`current input receipt stays native and saved while only prior identical input is evidence (dsh ${budgeted})`, async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, budgeted);
  t.after(() => host.close());
  const reader = new MemoryClient(host.config); await reader.connect();
  t.after(() => reader.close());
  const text = 'synthetic current-input boundary identical question';
  const first = await host.create('scope-first'); t.after(() => first.dispose());
  first.agent.followup(user('scope-one', text)); await first.agent.whenIdle();
  const earlier = host.memory.status(SessionId('scope-first')).latest_input!;
  assert.ok(!JSON.stringify(adapter.requests[0].messages.filter(message => message.role === 'user'
    && message.source && 'form' in message.source && message.source.form === 'snapshot')).includes(earlier.message_id));
  const second = await host.create('scope-second'); t.after(() => second.dispose());
  second.agent.followup(user('scope-two', text)); await second.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  const current = host.memory.status(SessionId('scope-second')).latest_input!;
  assert.equal((await reader.read(current.message_id)).event.text, text);
  const request = adapter.requests.at(-1)!;
  const section = request.messages.flatMap(message => message.role === 'user' && message.source
    && 'form' in message.source && message.source.form === 'snapshot' ? message.source.sections : [])
    .find(section => section.name === 'evernook-evidence');
  assert.ok(section);
  assert.ok(section.text.includes(earlier.message_id));
  assert.ok(!section.text.includes(current.message_id));
  assert.ok(request.messages.some(message => message.role === 'user'
    && message.content.some(block => block.type === 'text' && block.text === text)));
});

for (const mode of ['durable_text', 'durable_tools'] as const) test(`real dsh compaction and a fresh native resume restore source-backed work inside the shared token budget (${mode})`,
    { skip: mode === 'durable_tools' && Reflect.get(ToolRuntime, 'evernookPublicationVersion') !== 1 }, async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true, streamMode: mode, toolPublication: mode === 'durable_tools' });
  t.after(() => host.close());
  const sessionId = SessionId('work-' + 'a'.repeat(205));
  const handle = await host.create(sessionId);
  const goal = '合成任务：迁移查询模块，保留接口，最后运行契约回归。';
  adapter.reply = 'synthetic implementation progress '.repeat(500);
  handle.agent.followup(user('work-goal', goal));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  adapter.reply = '## Primary Request and Intent\n- 迁移查询模块，保留接口。\n## Current Work\n- 实现已保存。\n## Pending Jobs\n- 运行契约回归。\n## Next Step\n- 运行测试。';
  const compacted = await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  assert.ok(compacted);
  await host.memory.capture(handle.agent.session);
  const work = await host.memory.work(sessionId);
  assert.ok(work, JSON.stringify(host.memory.status(sessionId)));
  assert.equal(work.state, 'unknown', 'a model summary cannot prove task completion');
  assert.equal(work.goal.text, goal);
  assert.match(JSON.stringify(work.progress), /实现已保存/);
  assert.match(JSON.stringify(work.pending), /契约回归/);
  const id = work.checkpoint_id;
  await handle.dispose();
  await host.memory.close();
  const memory = await installMemory(host.ctx, host.config);
  t.after(() => memory.close());
  await memory.rescan(sessionId);
  assert.equal((await memory.work(sessionId))?.checkpoint_id, id);
  const resumed = await host.ctx.agents.resume({ resumeSessionId: sessionId,
    agentOptions: { provider: 'synthetic', model: 'local-test' } });
  t.after(() => resumed.dispose());
  adapter.reply = '合成：继续运行回归。';
  const current = '当前指令：只列出尚未完成的回归，不修改代码。';
  resumed.agent.followup(user('work-continue', current));
  await resumed.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  const request = adapter.requests.at(-1)!;
  const serialized = JSON.stringify(request);
  assert.match(serialized, /untrusted_work_continuation/);
  assert.ok(serialized.includes(id));
  assert.ok(serialized.includes(current));
  assert.doesNotMatch(serialized, /Treat the captured context as established background/);
  assert.ok(host.config.memoryBudget!.countRequest(request) + host.config.memoryBudget!.outputReserveTokens <= host.config.memoryBudget!.contextWindow);
});

test('native auxiliary compaction cannot bypass protected-history screening or the full request window', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true });
  t.after(() => host.close());
  const handle = await host.create('protected-compaction');
  t.after(() => handle.dispose());
  const secret = 'password=synthetic-compaction-history-123';
  handle.agent.followup(user('protected-before-compact', secret));
  await handle.agent.whenIdle();
  assert.equal(adapter.requests.length, 0);
  adapter.reply = 'synthetic safe work '.repeat(500);
  const safe = '合成安全任务：继续核对接口与回归。';
  handle.agent.followup(user('safe-before-compact', safe));
  await handle.agent.whenIdle();
  adapter.reply = '## Current Work\n- 合成安全进展。\n## Pending Jobs\n- 运行回归。';
  await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  const auxiliary = adapter.requests.filter(request => request.purpose === 'compaction');
  assert.equal(auxiliary.length, 1);
  assert.ok(!JSON.stringify(auxiliary).includes(secret));
  for await (const _ of host.ctx.llm.stream({ provider: 'synthetic', model: 'local-test', purpose: 'compaction',
    sessionId: handle.agent.session.id, maxTokens: 512, messages: [
      { role: 'user', content: [{ type: 'text', text: secret }] },
      { role: 'user', content: [{ type: 'text', text: safe }] },
    ] })) { /* A bounded auxiliary request may continue with the safe turn. */ }
  assert.ok(!JSON.stringify(adapter.requests.at(-1)).includes(secret));
  const before = adapter.requests.length;
  await assert.rejects(async () => {
    for await (const _ of host.ctx.llm.stream({ provider: 'synthetic', model: 'local-test', purpose: 'compaction',
      sessionId: handle.agent.session.id, maxTokens: 1000, messages: [
        { role: 'user', content: [{ type: 'text', text: safe }] },
        { role: 'user', content: [{ type: 'text', text: 'oversized request '.repeat(15000) }] },
      ] })) { /* Drain only if admission incorrectly succeeds. */ }
  }, /host_context_window_exceeded/);
  assert.equal(adapter.requests.length, before);
});

test('a native fork retains original checkpoint controls and cannot recreate a suppressed parent goal', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true });
  t.after(() => host.close());
  const parent = await host.create('work-parent');
  t.after(() => parent.dispose());
  const goal = '合成旧目标：修复紫藤查询接口。';
  adapter.reply = 'synthetic parent progress '.repeat(500);
  parent.agent.followup(user('fork-goal', goal));
  await parent.agent.whenIdle();
  adapter.reply = '## Current Work\n- 紫藤接口待检查。\n## Pending Jobs\n- 运行回归。';
  await host.ctx.compaction.compactNow(parent.agent, new AbortController().signal);
  await host.memory.capture(parent.agent.session);
  await host.ctx.sessions.flush(parent.agent.session);
  const stored = await host.ctx.sessionPersistence.open(parent.agent.session.id, 'read');
  const contents = await stored.read();
  await stored.close();
  const child = await host.ctx.agents.create({ sessionId: SessionId('work-child'),
    seed: buildForkSeed(contents.events, contents.events.at(-1)!.seq), inheritedEventCount: SessionLogOffset(contents.events.length),
    meta: { cwd: host.root, parentSession: parent.agent.session.id, isSeeded: true },
    agentOptions: { provider: 'synthetic', model: 'local-test' } });
  t.after(() => child.dispose());
  await host.memory.capture(child.agent.session);
  const inherited = await host.memory.work(child.agent.session.id);
  assert.ok(inherited, JSON.stringify(host.memory.status(child.agent.session.id)));
  assert.equal(inherited.goal.text, goal);
  const target = (await host.memory.work(parent.agent.session.id))!.goal.message_id;
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-control','main','confirm'),'user',1,None,'complete','合成用户确认：不再召回旧目标。')); r.change(a,Change('suppress-parent-goal',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))", host.config.config, target], { cwd: checkout });
  adapter.reply = '合成：仅核对状态。';
  child.agent.followup(user('child-current', '当前指令：仅核对来源控制状态。'));
  await child.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  assert.equal((await host.memory.work(child.agent.session.id))?.state, 'source_unavailable');
  assert.ok(!JSON.stringify(adapter.requests.at(-1)).includes('紫藤'));
});

test('a source invalidated during final request counting cannot enter the resumed provider', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true });
  t.after(() => host.close());
  const handle = await host.create('work-race');
  t.after(() => handle.dispose());
  adapter.reply = 'synthetic work race '.repeat(500);
  handle.agent.followup(user('race-goal', '合成旧目标：迁移雪松模块。'));
  await handle.agent.whenIdle();
  adapter.reply = '## Current Work\n- 雪松模块已迁移。\n## Pending Jobs\n- 运行回归。';
  await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  await host.memory.capture(handle.agent.session);
  const target = (await host.memory.work(handle.agent.session.id))!.goal.message_id;
  const counter = host.config.memoryBudget!.countRequest;
  let armed = true;
  host.config.memoryBudget!.countRequest = request => {
    if (armed && JSON.stringify(request).includes('untrusted_work_continuation')) {
      armed = false;
      execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-race','main','confirm'),'user',1,None,'complete','合成用户确认：不再召回旧目标。')); r.change(a,Change('suppress-race-goal',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))", host.config.config, target], { cwd: checkout });
    }
    return counter(request);
  };
  const before = adapter.requests.length;
  handle.agent.followup(user('race-next', '当前指令：仅核对来源状态。'));
  await handle.agent.whenIdle();
  assert.equal(armed, false);
  assert.equal(adapter.requests.length, before + 1);
  assert.deepEqual(host.errors, []);
  assert.ok(!JSON.stringify(adapter.requests.at(-1)).includes('雪松'));
  assert.match(JSON.stringify(adapter.requests.at(-1)), /source_unavailable/);
});

test('over-budget continuation fields disclose source ranges and remain available through ordinary deep reads', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true });
  t.after(() => host.close());
  const handle = await host.create('work-budget');
  t.after(() => handle.dispose());
  adapter.reply = 'synthetic completed stage '.repeat(500);
  handle.agent.followup(user('work-long-goal', '合成原始任务：保留所有原文。\n' + 'original task detail '.repeat(1000)));
  await handle.agent.whenIdle();
  adapter.reply = ['Primary Request and Intent', 'Key Technical Concepts', 'Files and Code', 'Errors and Fixes',
    'Pending Jobs', 'Current Work', 'Next Step', 'Critical Context']
    .map(title => '## ' + title + '\n- ' + 'synthetic engineering detail '.repeat(60)).join('\n');
  await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  await host.memory.capture(handle.agent.session);
  const work = await host.memory.work(handle.agent.session.id);
  assert.ok(work);
  assert.ok(work.pending.length);
  adapter.reply = '合成：仅返回状态与来源。';
  handle.agent.followup(user('work-budget-next', '当前只需要状态与遗漏来源。'));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  const request = adapter.requests.at(-1)!;
  const section = request.messages.flatMap(message => message.role === 'user' && message.source
    && 'form' in message.source && message.source.form === 'snapshot' ? message.source.sections : [])
    .find(section => section.name === 'evernook-evidence');
  assert.ok(section);
  const payload = JSON.parse(section.text);
  const restored = payload.items.find((item: { kind: string }) => item.kind === 'work_continuation').data;
  const omitted = restored.omitted.find((item: { field?: string; fields?: string[] }) => item.field === 'progress' || item.fields?.includes('progress'));
  assert.ok(omitted);
  assert.equal(omitted.reason, 'token_budget');
  assert.equal(omitted.message_id, work.progress[0].message_id);
  assert.ok(restored.progress.length < work.progress.length);
  const reader = new MemoryClient(host.config);
  await reader.connect();
  try {
    const original = await reader.read(omitted.message_id);
    const excerpt = Array.from(original.event.text).slice(omitted.start, omitted.end).join('');
    assert.ok(work.progress.some(item => item.start >= omitted.start && item.end <= omitted.end && excerpt.includes(item.text)));
  } finally { await reader.close(); }
  assert.ok(host.target!.encode(section.text, { add_special_tokens: false }).ids.length <= host.config.memoryBudget!.memoryTokens);
  assert.ok(host.config.memoryBudget!.countRequest(request) + host.config.memoryBudget!.outputReserveTokens <= host.config.memoryBudget!.contextWindow);
  const before = adapter.requests.length;
  host.config.memoryBudget!.memoryTokens = 256;
  const tiny = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
  tiny.policy.normal_tokens = 128;
  await writeFile(host.config.contextConfig!, JSON.stringify(tiny));
  handle.agent.followup(user('work-minimum-budget', '当前指令：仅返回状态，不猜测遗漏。'));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  assert.equal(adapter.requests.length, before + 1);
  assert.ok((host.memory.status(handle.agent.session.id).context?.provider_payload_tokens ?? Infinity) <= 256);
  assert.ok(host.memory.status(handle.agent.session.id).context?.degraded.includes('context_envelope_over_budget'));
});

test('a later partial compaction inherits sources from the earlier continuation actually supplied to its summarizer', async t => {
  const adapter = new Synthetic();
  adapter.toolOnce = true;
  const host = await boot(adapter, true, false, { compaction: true });
  t.after(() => host.close());
  host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic continuation source',
    parameters: { amount: { type: 'number' } }, execute: async () => [{ type: 'text', text: '合成工具旧结论：采用南星方案。' }] }));
  const handle = await host.create('work-consumption');
  t.after(() => handle.dispose());
  adapter.reply = 'synthetic first progress '.repeat(400);
  handle.agent.followup(user('consumption-goal', '合成任务：核对工具结果并继续工作。'));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  adapter.reply = '## Current Work\n- 南星方案待复核。\n## Pending Jobs\n- 运行回归。';
  const first = await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  assert.ok(first);
  await host.memory.capture(handle.agent.session);
  const earlier = await host.memory.work(handle.agent.session.id);
  assert.ok(earlier);
  adapter.reply = 'synthetic later progress '.repeat(400);
  handle.agent.followup(user('partial-start', '合成局部任务：核对项目说明。'));
  await handle.agent.whenIdle();
  await host.ctx.sessions.flush(handle.agent.session);
  const stored = await host.ctx.sessionPersistence.open(handle.agent.session.id, 'read');
  const contents = await stored.read();
  await stored.close();
  const start = contents.events.find(event => event.type === 'user/message' && String(event.data.id) === 'partial-start');
  const end = contents.events.findLast(event => event.type === 'assistant/message');
  assert.ok(start && end);
  adapter.reply = '## Current Work\n- 续接中的南星方案仍待复核；已核对项目说明。\n## Pending Jobs\n- 运行回归。';
  const results: CompactionResult[] = [];
  const stop = host.ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    if (agent === handle.agent && !results.length) {
      results.push(await host.ctx.compaction.compactRegion(start.seq, end.seq, agent, signal));
      adapter.reply = '合成：局部续接已保存。';
    }
    return next();
  });
  handle.agent.followup(user('trigger-partial', '合成当前指令：继续检查局部任务。'));
  await handle.agent.whenIdle();
  stop();
  assert.deepEqual(host.errors, []);
  assert.equal(results.length, 1);
  const second = results[0];
  const oldNode = contents.events.find(event => event.type === 'user/message'
    && event.data.source.kind === 'compact-checkpoint' && event.data.source.compactionId === first.compactionId);
  assert.ok(oldNode);
  assert.ok(!second.shadowedSeqs.includes(oldNode.seq));
  assert.ok(JSON.stringify(adapter.requests.filter(request => request.purpose === 'compaction').at(-1)).includes(earlier.checkpoint_id));
  await host.memory.capture(handle.agent.session);
  assert.notEqual((await host.memory.work(handle.agent.session.id))?.checkpoint_id, earlier.checkpoint_id);
  const tool = host.rows().find(row => row.event.role === 'tool' && row.event.text.includes('合成工具旧结论'));
  assert.ok(tool);
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-consumption','main','confirm'),'user',1,None,'complete','合成用户确认：不再召回旧工具结果。')); r.change(a,Change('suppress-consumed-tool',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))", host.config.config, tool.receipt.message_id], { cwd: checkout });
  assert.equal((await host.memory.work(handle.agent.session.id))?.state, 'source_unavailable');
});

test('changed task intent is retained as untrusted work while the initial user request is explicitly labelled', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { compaction: true });
  t.after(() => host.close());
  const handle = await host.create('work-intent');
  t.after(() => handle.dispose());
  adapter.reply = 'synthetic earlier task '.repeat(400);
  handle.agent.followup(user('initial-intent', '合成初始请求：核对旧接口。'));
  await handle.agent.whenIdle();
  handle.agent.followup(user('changed-intent', '旧接口任务结束；当前任务改为检查银杉新模块，然后运行类型检查。'));
  await handle.agent.whenIdle();
  adapter.reply = '## Primary Request and Intent\n- 当前任务是检查银杉新模块；旧接口任务已结束。\n## Current Work\n- 新模块实现待核对。\n## Pending Jobs\n- 运行类型检查。\n## Critical Context\n- 当前用户要求只处理新模块。';
  await host.ctx.compaction.compactNow(handle.agent, new AbortController().signal);
  await host.memory.capture(handle.agent.session);
  const work = await host.memory.work(handle.agent.session.id);
  assert.ok(work);
  assert.equal(work.goal_role, 'initial_user_request');
  assert.equal(work.goal.text, '合成初始请求：核对旧接口。');
  assert.match(JSON.stringify(work.progress), /当前任务是检查银杉新模块/);
  assert.match(JSON.stringify(work.progress), /当前用户要求只处理新模块/);
  assert.equal(work.state, 'unknown', 'generated intent cannot certify completion');
  adapter.reply = '合成：仅列新模块待办。';
  handle.agent.followup(user('intent-current', '当前指令：只按新模块目标列待办。'));
  await handle.agent.whenIdle();
  assert.deepEqual(host.errors, []);
  const request = JSON.stringify(adapter.requests.at(-1));
  assert.match(request, /initial_user_request/);
  assert.match(request, /当前任务是检查银杉新模块/);
  assert.match(request, /只按新模块目标列待办/);
});

test('the saved native compaction policy controls actual pressure triggers and reports the selected limits', async () => {
  class Windowed extends Synthetic {
    override async resolveModel(provider: string, model: string, signal?: AbortSignal) {
      return { ...await super.resolveModel(provider, model, signal), context: { contextWindow: 20000 } };
    }
  }
  for (const auto of [false, true]) {
    const adapter = new Windowed();
    const policy = { auto, thresholdRatio: 0.2, retainTokens: 0, headroomTokens: 512, maxTokens: 512 };
    const host = await boot(adapter, true, true, { compaction: policy });
    const handle = await host.create('native-policy-' + auto);
    adapter.inspect = request => { adapter.reply = request.purpose === 'compaction'
      ? '## Current Work\n- 合成进展。\n## Pending Jobs\n- 运行回归。' : 'synthetic policy progress '.repeat(400); };
    try {
      handle.agent.followup(user('policy-first', '合成任务：验证配置。 ' + 'task policy evidence '.repeat(1000)));
      await handle.agent.whenIdle();
      handle.agent.followup(user('policy-next', '合成当前指令：继续回归。'));
      await handle.agent.whenIdle();
      assert.deepEqual(host.errors, []);
      assert.equal(adapter.requests.some(request => request.purpose === 'compaction'), auto);
      const settings = await host.memory.settings();
      assert.equal(settings.host?.compaction.base_policy.auto, auto);
      assert.equal(settings.host?.compaction.base_policy.thresholdRatio, 0.2);
      assert.equal(settings.host?.compaction.base_policy.maxTokens, 512);
      assert.equal(settings.host?.compaction.trigger_measurement, 'native_backend');
      assert.equal(settings.host?.context_window, 20000);
      assert.equal(settings.host?.output_reserve_tokens, 1000);
      for (const request of adapter.requests) assert.ok(host.config.memoryBudget!.countRequest(request) + 1000 <= 20000);
    } finally { await handle.dispose(); await host.close(); }
  }
});

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

test('independent service paragraph profile corrections reach the next actual provider request', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { service: true });
  t.after(() => host.close());
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  function cli(command: string, input: Record<string, unknown> = {}) {
    return JSON.parse(execFileSync(python!, ['-B', '-m', 'evernook.cli', command, '--config', host.config.config,
      '--access', owner], { cwd: checkout, input: JSON.stringify(input), encoding: 'utf8' })).result;
  }
  const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
  Object.assign(saved.policy, { normal_tokens: 4096, deep_tokens: 8192, profile_tokens: 1000,
    fact_keys: [['user', 'preference:answer_verbosity']] });
  await writeFile(host.config.contextConfig!, JSON.stringify(saved));
  host.config.memoryBudget!.memoryTokens = 4096;
  cli('preference-update-configure', { enabled: true,
    admitted_processor: cli('preference-update-status').settings.rules_processor });
  async function turn(session: string, text: string) {
    const handle = await host.create(session); t.after(() => handle.dispose());
    handle.agent.followup(user(session + '-input', text)); await handle.agent.whenIdle();
    return host.memory.status(handle.agent.session.id).latest_input!.message_id;
  }
  function items() {
    const block = adapter.requests.at(-1)!.messages.flatMap(m => m.content)
      .find(b => b.type === 'text' && b.text.includes('untrusted_memory_context'));
    assert.ok(block?.type === 'text');
    return JSON.parse(block.text.slice(block.text.indexOf('{'))).items as
      { kind: string; data: { label?: string; fact?: { text: string } | null } }[];
  }
  const first = await turn('paragraph-first', '今天聊了很久。说到日常交流，我偏好简短回答。');
  cli('preference-update-process', { source_ids: [first] });
  const temporary = await turn('paragraph-temporary', '这次，我偏好详细回答。');
  assert.ok(items().some(i => i.kind === 'profile' && i.data.label === '我偏好简短回答。'));
  cli('preference-update-process', { source_ids: [temporary] });
  assert.equal(cli('profile-summary').tags[0].draft.label, '我偏好简短回答。');
  const incomingId = await turn('paragraph-new', '最近重新想过。现在我偏好详细回答。');
  const incoming = cli('preference-update-process', { source_ids: [incomingId] }).updates[0];
  assert.equal(incoming.state, 'pending_clarification');
  const confirmation = await turn('paragraph-confirm', '合成：确认详细回答是我的真实偏好，更正之前说法。');
  assert.equal(items().some(i => i.kind === 'profile'), false);
  const fact = cli('fact-query', { subject_id: 'user', predicate: incoming.predicate });
  const profile = cli('profile-read', { tag_id: incoming.profile_tag_id });
  cli('preference-resolve', { subject_id: 'user', predicate: incoming.predicate,
    profile_tag_id: incoming.profile_tag_id, clarification_id: incoming.clarification_id,
    confirmation_id: confirmation, expected_fact_revision: fact.knowledge_revision,
    expected_profile_revision: profile.revision, kind: 'correction', choice: 'incoming' });
  await turn('paragraph-after', '合成：继续聊天。');
  assert.ok(items().some(i => i.kind === 'profile' && i.data.label === '我偏好详细回答。'));
  assert.ok(items().some(i => i.kind === 'facts' && i.data.fact?.text === '我偏好详细回答。'));
  assert.equal(items().some(i => (i.kind === 'profile' && i.data.label === '我偏好简短回答。')
    || (i.kind === 'facts' && i.data.fact?.text === '我偏好简短回答。')), false);
  assert.deepEqual(host.errors, []);
});

test('independent service topic views reach native provider and source withdrawal excludes old body', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { service: true });
  t.after(() => host.close());
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  function cli(command: string, input: Record<string, unknown> = {}) {
    return JSON.parse(execFileSync(python!, ['-B', '-m', 'evernook.cli', command, '--config', host.config.config,
      '--access', owner], { cwd: checkout, input: JSON.stringify(input), encoding: 'utf8' })).result;
  }
  async function turn(session: string, text: string) {
    const handle = await host.create(session); t.after(() => handle.dispose());
    handle.agent.followup(user(session + '-input', text)); await handle.agent.whenIdle();
    return host.memory.status(handle.agent.session.id).latest_input!.message_id;
  }
  function items() {
    const block = adapter.requests.at(-1)!.messages.flatMap(m => m.content)
      .find(b => b.type === 'text' && b.text.includes('untrusted_memory_context'));
    assert.ok(block?.type === 'text');
    return JSON.parse(block.text.slice(block.text.indexOf('{'))).items as
      { kind: string; data: { view_id?: string; content?: { citations: { quote: string }[] } } }[];
  }
  const original = '合成：我们在桥北湿地见过白鹭。';
  const source = await turn('topic-history', original);
  cli('topic-view-define', { request_id: 'define-wetland', view_id: 'wetland', expected_revision: 0,
    definition: { title: '湿地共同经历', purpose: '给未来聊天提供可回源的经历背景', query: '湿地' } });
  const saved = JSON.parse(await readFile(host.config.contextConfig!, 'utf8'));
  Object.assign(saved.policy, { normal_tokens: 8192, deep_tokens: 16384,
    topic_view_ids: ['wetland'], topic_view_tokens: 5000 });
  await writeFile(host.config.contextConfig!, JSON.stringify(saved));
  host.config.memoryBudget!.memoryTokens = 8192;
  assert.equal(cli('topic-view-refresh', { view_id: 'wetland', timeout: 10 }).state, 'valid');
  const confirmation = await turn('topic-with-view', '合成：继续聊共同经历。');
  assert.ok(items().some(i => i.kind === 'topic_view' && i.data.view_id === 'wetland'
    && i.data.content?.citations.some(c => c.quote === original)), JSON.stringify({ items: items(),
      viewStateAfterTurn: cli('topic-view-read', { view_id: 'wetland', current_input_id: confirmation }).state }));
  cli('change', { request_id: 'hide-topic-source', target_id: source, expected_version: 1,
    action: 'suppress', evidence_id: confirmation, reason: 'synthetic topic control' });
  await turn('topic-withdrawn', '合成：再聊湿地。');
  assert.equal(items().some(i => i.kind === 'topic_view'), false);
  assert.doesNotMatch(JSON.stringify(adapter.requests.at(-1)), /桥北湿地见过白鹭/);
  const updated = '合成：更正后的经历在银叶桥湿地，见过苍鹭。';
  cli('topic-view-edit', { request_id: 'edit-wetland', view_id: 'wetland', expected_revision: 1, text: updated });
  assert.equal(cli('topic-view-refresh', { view_id: 'wetland', timeout: 10 }).state, 'valid');
  await turn('topic-updated', '合成：继续。');
  assert.ok(items().some(i => i.kind === 'topic_view' && i.data.content?.citations.some(c => c.quote === updated)));
  assert.deepEqual(host.errors, []);
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

for (const mode of ['durable_text', 'durable_tools'] as const) test(`budgeted durable text preserves split UTF-16 pairs and splits large provider deltas at the core byte limit (${mode})`,
    { skip: mode === 'durable_tools' && Reflect.get(ToolRuntime, 'evernookPublicationVersion') !== 1 }, async () => {
  const adapter = new Synthetic();
  const text = '安全🙂' + '字'.repeat(22000) + '。';
  adapter.chunks = [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: '安全\uD83D' },
    { type: 'text-delta', index: 0, text: '\uDE42' + '字'.repeat(22000) + '。' },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ];
  const host = await boot(adapter, true, true, { streamMode: mode, toolPublication: mode === 'durable_tools' });
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
    assert.ok(state?.degraded.includes('query_coverage_limited'));
    assert.equal(state?.complete, false);
    const latest = adapter.requests.at(-1)!;
    const memories = latest.messages.flatMap(message => message.content).filter(block => block.type === 'text')
      .filter(block => block.text.includes('untrusted_memory_context'));
    assert.equal(memories.length, 1);
    assert.ok(memories.reduce((sum, block) => sum + host.target!.encode(block.text, { add_special_tokens: false }).ids.length, 0) <= 512);
  } finally { await handle.dispose(); await host.close(); }
});

test('long native input recalls its tail question through the independent service in both budget modes', async () => {
  for (const budgeted of [false, true]) {
    const adapter = new Synthetic();
    const host = await boot(adapter, budgeted, false, { service: true });
    const handle = await host.create('long-tail-history');
    const ordinary = new MemoryClient(host.config);
    await ordinary.connect();
    try {
      const receipt = await ordinary.call<{ message_id: string }>('remember', { event: {
        source: { entry: 'dsh', session: 'synthetic-history', branch: 'main', event: 'tail-target' },
        role: 'user', sequence: 1, occurred_at: '2019-01-01T00:00:00Z', completion: 'complete',
        text: '合成：跨年约定是蓝色风铃。',
      } });
      handle.agent.followup(user('long-tail-input', '无关背景。'.repeat(100) + '跨年约定'));
      await handle.agent.whenIdle();
      assert.deepEqual(host.errors, []);
      assert.equal(adapter.requests.length, 1);
      const delivered = JSON.stringify(adapter.requests[0]);
      assert.ok(delivered.includes(receipt.message_id));
      assert.ok(delivered.includes('蓝色风铃'));
      const originals = host.rows().filter(row => row.event.role === 'user');
      assert.ok(originals.some(row => row.event.text === '无关背景。'.repeat(100) + '跨年约定'));
      if (budgeted) assert.ok(host.memory.status(handle.agent.session.id).context?.degraded.includes('query_coverage_limited'));
    } finally { await ordinary.close(); await handle.dispose(); await host.close(); }
  }
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


const patchedPublication = Reflect.get(ToolRuntime, 'evernookPublicationVersion') === 1;

test('reasoning tool stream saves complete frames before disclosure and preserves replay on the next model request', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic();
  const id = ToolCallId('reasoning-tool');
  const reasoning = '合成推理🙂：只核对临时证据。';
  const frames: StreamChunk[] = [
    { type: 'block-start', index: 0, blockType: 'reasoning' },
    { type: 'reasoning-delta', index: 0, text: '合成推理\uD83D' },
    { type: 'reasoning-delta', index: 0, text: '\uDE42：只核对临时证据。' },
    { type: 'block-end', index: 0, block: { type: 'reasoning', text: reasoning } },
    { type: 'block-start', index: 1, blockType: 'text' },
    { type: 'text-delta', index: 1, text: '合成可见答案。' },
    { type: 'block-end', index: 1, block: { type: 'text', text: '合成可见答案。' } },
    { type: 'block-start', index: 2, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 2, id, name: 'synthetic-tool', argumentsDelta: '{"amount":37.50}' },
    { type: 'block-end', index: 2, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: '{"amount":37.50}' } },
    { type: 'finish', reason: { kind: 'tool-calls' }, replayState: { response: { id: 'synthetic-reasoning-response' },
      blocks: [{ signature: 'synthetic-replay-signature' }, {}, {}] } },
  ];
  adapter.chunks = frames;
  const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
  const reader = new MemoryClient(host.config); await reader.connect();
  const handle = await host.create('reasoning-complete');
  const visible: StreamChunk[] = []; let effects = 0; let checked = false;
  host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(frame.chunk); });
  adapter.afterChunk = async index => {
    if (index === 8) { assert.equal(visible.length, 0); assert.equal(effects, 0); checked = true; }
  };
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic reasoning workflow',
      parameters: { amount: { type: 'number' } }, execute: async () => {
        const result = await reader.call<{ hits: { message_id: string }[] }>('search', { query: 'evernook_dsh_tool_frames_v1', mode: 'exact', limit: 100 });
        assert.equal(result.hits.length, 1);
        const capture = await reader.readCapture(result.hits[0].message_id);
        assert.equal(capture.complete, true); assert.equal(capture.input_id, host.memory.status(handle.agent.session.id).latest_input!.message_id);
        const saved = JSON.parse(execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import MemoryRuntime; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); sys.stdout.buffer.write(r.read_attachment(a,sys.argv[3],'raw-tool-stream-v1'))", host.config.config, host.config.access, result.hits[0].message_id], { cwd: checkout, encoding: 'utf8' }));
        assert.deepEqual(saved, frames);
        const attempts = await host.memory.streams(handle.agent.session.id);
        assert.equal((await reader.readStream(attempts.streams[0])).text, '合成可见答案。');
        effects++; adapter.chunks = undefined;
        return [{ type: 'text', text: '合成工具完成。' }];
      } }));
    handle.agent.followup(user('reasoning-input', '合成：推理、文本与工具分开保存。'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []); assert.equal(checked, true); assert.equal(effects, 1);
    assert.equal(adapter.requests.length, 2);
    assert.match(JSON.stringify(adapter.requests[1]), /synthetic-replay-signature/);
    assert.equal(visible.filter(value => value.type === 'reasoning-delta').map(value => value.text).join(''), reasoning);
  } finally { await reader.close(); await handle.dispose(); await host.close(); }
});

for (const failure of ['protected', 'cross-channel', 'mismatch', 'unclosed', 'canceled', 'interrupted', 'oversized', 'save-revoked']) {
  test(`reasoning tool stream blocks ${failure} before reasoning disclosure or tool execution`, { skip: !patchedPublication }, async () => {
    const adapter = new Synthetic();
    const reasoning = failure === 'protected' ? 'password=synthetic-reasoning-secret'
      : failure === 'cross-channel' ? 'pass' : failure === 'oversized' ? 'x'.repeat(800000) : '合成推理。';
    const id = ToolCallId('reasoning-failure-call');
    adapter.chunks = [
      { type: 'block-start', index: 0, blockType: 'reasoning' },
      { type: 'reasoning-delta', index: 0, text: reasoning },
    ];
    if (failure !== 'unclosed') adapter.chunks.push({ type: 'block-end', index: 0,
      block: { type: 'reasoning', text: failure === 'mismatch' ? 'changed' : reasoning } });
    if (failure === 'cross-channel') adapter.chunks.push({ type: 'block-start', index: 1, blockType: 'text' },
      { type: 'text-delta', index: 1, text: 'word=synthetic-reasoning-secret' },
      { type: 'block-end', index: 1, block: { type: 'text', text: 'word=synthetic-reasoning-secret' } });
    adapter.chunks.push({ type: 'block-start', index: 2, blockType: 'tool-call' },
      { type: 'tool-call-delta', index: 2, id, name: 'synthetic-tool', argumentsDelta: '{"amount":37.50}' },
      { type: 'block-end', index: 2, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: '{"amount":37.50}' } },
      { type: 'finish', reason: { kind: 'tool-calls' } });
    const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
    const handle = await host.create('reasoning-failure-' + failure);
    const visible: StreamChunk[] = []; let effects = 0;
    host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(frame.chunk); });
    adapter.afterChunk = async index => {
      if (index !== 1) return;
      if (failure === 'canceled') handle.agent.cancel({ kind: 'user' });
      if (failure === 'interrupted') throw new Error('synthetic protocol interruption');
      if (failure === 'save-revoked') execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; c=Path(sys.argv[1]); pair_client(c,client_id='synthetic-dsh',entry='dsh',access_path=c.parent/'mcp-access-dsh.json',scopes=('recall',))", host.config.config], { cwd: checkout });
    };
    try {
      host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic blocked reasoning',
        parameters: { amount: { type: 'number' } }, execute: async () => { effects++; adapter.chunks = undefined; return []; } }));
      handle.agent.followup(user('reasoning-failure-input', '合成：失败不展示推理或运行工具。'));
      await handle.agent.whenIdle();
      assert.equal(effects, 0); assert.equal(visible.length, 0);
      if (failure !== 'canceled') assert.ok(host.errors.length);
      assert.doesNotMatch(host.errors.map(String).join('\n'), /synthetic-reasoning-secret/);
      const streams = await host.memory.streams(handle.agent.session.id);
      const saved = await host.memory.readStream(streams.streams[0]);
      assert.equal(saved.complete, false); assert.equal(saved.final_message_id, null);
      if (failure === 'canceled') assert.equal(saved.state, 'canceled');
    } finally { await handle.dispose(); await host.close(); }
  });
}
test('patched tool publication saves the exact final snapshot before native notification', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic(); adapter.toolOnce = true;
  const host = await boot(adapter, false, false, { toolPublication: true });
  const reader = new MemoryClient(host.config); await reader.connect();
  const handle = await host.create('publication-exact');
  const observed: Promise<void>[] = [];
  const finalText = '合成最终修整🙂\r\n37.50元。';
  try {
    host.ctx.tools.register({ ...defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic final content',
      parameters: { amount: { type: 'number' } }, execute: async () => [{ type: 'text', text: 'before final transform' }] }),
      finalizeContent: () => [{ type: 'text', text: finalText }] });
    host.ctx.on('tools/result', () => { observed.push((async () => {
      const found = await reader.call<{ hits: { message_id: string; source: { event: string }; role: string }[] }>('search',
        { query: 'evernook_tool_publication_v1', mode: 'exact', limit: 100 });
      const hit = found.hits.find(value => value.role === 'tool' && value.source.event.startsWith('tool-publish-v1:') && value.source.event.endsWith(':result'));
      assert.ok(hit, 'Final result must already be in the real ledger');
      const stored = await reader.read(hit.message_id);
      const snapshot = JSON.parse(stored.event.text) as { result: { content: { text: string }[]; value: unknown } };
      assert.equal(snapshot.result.content[0].text, finalText);
      assert.deepEqual(snapshot.result.value, [{ type: 'text', text: 'before final transform' }]);
      const manifest = JSON.parse(execFileSync(python!, ['-c', "import json,sys; from pathlib import Path; from dataclasses import asdict; from evernook import pair_client,MemoryRuntime; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); print(json.dumps(asdict(MemoryRuntime(c).read_capture(a,sys.argv[2]))))", host.config.config, hit.message_id], { cwd: checkout, encoding: 'utf8' })) as { input_id: string; tool_name: string; complete: boolean };
      assert.equal(manifest.input_id, host.memory.status(handle.agent.session.id).latest_input!.message_id);
      assert.equal(manifest.tool_name, 'synthetic-tool');
      assert.equal(manifest.complete, true);
    })()); void observed.at(-1)!.catch(() => {}); });
    handle.agent.followup(user('publication-exact-input', '合成：验证最终工具结果先保存再通知。'));
    await handle.agent.whenIdle();
    assert.equal(observed.length, 1); await Promise.all(observed);
    assert.deepEqual(host.errors, []);
    assert.equal(adapter.requests.length, 2);
  } finally { await reader.close(); await handle.dispose(); await host.close(); }
});

test('patched tool publication blocks after the real capture identity is revoked following a side effect', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic(); adapter.toolOnce = true;
  const host = await boot(adapter, false, false, { toolPublication: true });
  const handle = await host.create('publication-save-failure');
  let effects = 0;
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic permission failure',
      parameters: { amount: { type: 'number' } }, execute: async () => {
        effects++;
        execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; c=Path(sys.argv[1]); pair_client(c,client_id='synthetic-dsh',entry='dsh',access_path=c.parent/'mcp-access-dsh.json',scopes=('recall',))", host.config.config], { cwd: checkout });
        return [{ type: 'text', text: 'synthetic result must remain unpublished' }];
      } }));
    handle.agent.followup(user('publication-failure-input', '合成：执行后撤销保存权限。'));
    await handle.agent.whenIdle();
    assert.equal(effects, 1);
    assert.equal(adapter.requests.length, 1);
    assert.equal(handle.agent.session.snapshotEvents().filter(value => value.type === 'tool/result').length, 0);
    assert.equal(handle.agent.session.snapshotEvents().filter(value => pluginRecordOf(value)?.type === 'plugin:dsh-evernook/publication-blocked').length, 1);
    handle.agent.followup(user('publication-no-retry', '合成：核对状态，不得自动重复副作用。'));
    await handle.agent.whenIdle();
    assert.equal(effects, 1); assert.equal(adapter.requests.length, 1);
  } finally { await handle.dispose(); await host.close(); }
});


for (const failure of ['protected', 'oversized']) test(`patched tool publication blocks ${failure} final snapshots`, { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic(); adapter.toolOnce = true;
  const host = await boot(adapter, false, false, { toolPublication: true });
  const reader = new MemoryClient(host.config); await reader.connect();
  const handle = await host.create('publication-' + failure);
  let effects = 0;
  try {
    const resultText = failure === 'protected' ? 'password=synthetic-tool-never-publish' : 'x'.repeat(600000);
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic unsafe result',
      parameters: { amount: { type: 'number' } }, execute: async () => { effects++; return [{ type: 'text', text: resultText }]; } }));
    handle.agent.followup(user('publication-' + failure + '-input', '合成：验证不安全结果阻断。'));
    await handle.agent.whenIdle();
    assert.equal(effects, 1); assert.equal(adapter.requests.length, 1);
    assert.equal(handle.agent.session.snapshotEvents().filter(value => value.type === 'tool/result').length, 0);
    assert.equal(handle.agent.session.snapshotEvents().filter(value => pluginRecordOf(value)?.type === 'plugin:dsh-evernook/publication-blocked').length, 1);
    if (failure === 'protected') {
      await assert.rejects(reader.call('search', { query: resultText, mode: 'exact', limit: 100 }), /evernook:protected_query/);
      const found = await reader.call<{ hits: unknown[] }>('search', { query: 'synthetic-tool-never-publish', mode: 'exact', limit: 100 });
      assert.equal(found.hits.length, 0);
    }
  } finally { await reader.close(); await handle.dispose(); await host.close(); }
});


for (const streamMode of ['buffered', 'durable_tools'] as const) test(`tool publication refuses an unpatched host before starting the memory transport (${streamMode})`, { skip: patchedPublication }, async () => {
  const ctx = new Context();
  try {
    await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
    await ctx.plugin(ToolRuntime).await();
    await assert.rejects(installMemory(ctx, { python: python!, config: join(output!, 'unused-runtime.json'),
      access: join(output!, 'unused-access.json'), entry: 'dsh', streamMode, toolPublication: true }), /evernook:tool_publication_patch_required/);
  } finally { await ctx.fiber.dispose(); }
});

test('mixed reliable mode refuses missing tool publication before starting the memory transport', async () => {
  const ctx = new Context();
  try {
    await assert.rejects(installMemory(ctx, { python: python!, config: join(output!, 'unused-runtime.json'),
      access: join(output!, 'unused-access.json'), entry: 'dsh', streamMode: 'durable_tools' }), /evernook:invalid_host_config/);
  } finally { await ctx.fiber.dispose(); }
});

test('mixed durable stream saves visible text before provider finish and saves exact tool frames before execution', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic();
  const id = ToolCallId('mixed-call');
  const frames: StreamChunk[] = [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: '合成前缀🙂。' },
    { type: 'block-end', index: 0, block: { type: 'text', text: '合成前缀🙂。' } },
    { type: 'block-start', index: 1, blockType: 'text' },
    { type: 'text-delta', index: 1, text: '37.50元。\r\n' },
    { type: 'block-end', index: 1, block: { type: 'text', text: '37.50元。\r\n' } },
    { type: 'block-start', index: 2, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 2, id, name: 'synthetic-tool', argumentsDelta: '{"amount":37.50}' },
    { type: 'block-end', index: 2, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: '{"amount":37.50}' } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ];
  adapter.chunks = frames;
  const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
  const reader = new MemoryClient(host.config); await reader.connect();
  const handle = await host.create('mixed-complete');
  const visible: StreamChunk[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(frame.chunk); });
  let effects = 0;
  let liveChecked = false;
  adapter.afterChunk = async index => {
    if (index === 1) {
      assert.equal(visible.filter(value => value.type === 'text-delta').map(value => value.text).join(''), '合成前缀🙂。');
      const found = await host.memory.streams(handle.agent.session.id);
      assert.equal(found.streams.length, 1);
      const saved = await reader.readStream(found.streams[0]);
      assert.equal(saved.text, '合成前缀🙂。'); assert.equal(saved.state, 'open');
      liveChecked = true;
    }
    if (index === 7) {
      assert.equal(visible.some(value => value.type === 'tool-call-delta'), false);
      assert.equal(effects, 0);
    }
  };
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic mixed workflow',
      parameters: { amount: { type: 'number' } }, execute: async () => {
        const found = await reader.call<{ hits: { message_id: string; source: { session: string } }[] }>('search',
          { query: 'evernook_dsh_tool_frames_v1', mode: 'exact', limit: 100 });
        const hit = found.hits.find(value => value.source.session === String(handle.agent.session.id)); assert.ok(hit);
        const saved = JSON.parse(execFileSync(python!, ['-c', "import json,sys; from pathlib import Path; from evernook import MemoryRuntime; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); sys.stdout.buffer.write(r.read_attachment(a,sys.argv[3],'raw-tool-stream-v1'))", host.config.config, host.config.access, hit.message_id], { cwd: checkout, encoding: 'utf8' }));
        assert.deepEqual(saved, frames);
        const attempts = await host.memory.streams(handle.agent.session.id);
        const complete = await reader.readStream(attempts.streams[0]);
        assert.equal(complete.state, 'complete'); assert.equal(complete.text, '合成前缀🙂。37.50元。\r\n');
        effects++; adapter.chunks = undefined;
        return [{ type: 'text', text: '合成工具结果。' }];
      } }));
    handle.agent.followup(user('mixed-input', '合成：可靠文本加工具。'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []); assert.equal(liveChecked, true); assert.equal(effects, 1);
    assert.equal(adapter.requests.length, 2);
    assert.equal(visible.filter(value => value.type === 'tool-call-delta').length, 1);
  } finally { await reader.close(); await handle.dispose(); await host.close(); }
});

test('mixed durable stream never publishes unsaved provider fields attached to live text frames', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic();
  adapter.chunks = [
    Object.assign({ type: 'block-start' as const, index: 0, blockType: 'text' as const }, { providerHint: 'password=synthetic-metadata-secret' }),
    Object.assign({ type: 'text-delta' as const, index: 0, text: '合成安全前缀。' }, { providerHint: 'password=synthetic-metadata-secret' }),
    { type: 'block-end', index: 0, block: { type: 'text', text: '合成安全前缀。' } },
    { type: 'finish', reason: { kind: 'stop' } },
  ];
  const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
  const handle = await host.create('mixed-extra-fields');
  const visible: StreamChunk[] = [];
  host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(frame.chunk); });
  try {
    handle.agent.followup(user('mixed-metadata-input', '合成：不能展示尚未保存的额外字段。'));
    await handle.agent.whenIdle();
    assert.doesNotMatch(JSON.stringify(visible), /providerHint|synthetic-metadata-secret/);
    assert.equal(visible.filter(value => value.type === 'text-delta').map(value => value.text).join(''), '合成安全前缀。');
    assert.equal(visible.some(value => value.type === 'finish'), false);
    assert.ok(host.errors.length);
    const found = await host.memory.streams(handle.agent.session.id);
    const saved = await host.memory.readStream(found.streams[0]);
    assert.equal(saved.state, 'failed'); assert.equal(saved.final_message_id, null);
  } finally { await handle.dispose(); await host.close(); }
});

test('mixed durable stream rejects duplicate provider call identities before any side effect', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic();
  const id = ToolCallId('duplicate-mixed-call');
  adapter.chunks = [0, 1].flatMap((index): StreamChunk[] => [
    { type: 'block-start', index, blockType: 'tool-call' },
    { type: 'tool-call-delta', index, id, name: 'synthetic-tool', argumentsDelta: '{"amount":37.50}' },
    { type: 'block-end', index, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: '{"amount":37.50}' } },
  ]);
  adapter.chunks.push({ type: 'finish', reason: { kind: 'tool-calls' } });
  const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
  const handle = await host.create('mixed-duplicate-call');
  let effects = 0;
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic duplicate identity',
      parameters: { amount: { type: 'number' } }, execute: async () => { effects++; adapter.chunks = undefined; return [{ type: 'text', text: 'synthetic result' }]; } }));
    handle.agent.followup(user('mixed-duplicate-input', '合成：重复调用身份必须阻断。'));
    await handle.agent.whenIdle();
    assert.equal(effects, 0);
    assert.equal(handle.agent.session.snapshotEvents().filter(value => value.type === 'tool/call').length, 0);
    assert.ok(host.errors.length);
  } finally { await handle.dispose(); await host.close(); }
});

for (const failure of ['cross-block', 'escaped-arguments', 'malformed-arguments', 'canceled', 'interrupted', 'truncated', 'mismatch', 'oversized', 'save-revoked', 'reasoning']) {
  test(`mixed durable stream preserves only saved prefixes on ${failure}`, { skip: !patchedPublication }, async () => {
    const adapter = new Synthetic();
    const prefix = '合成前缀🙂。';
    const first = prefix + (failure === 'cross-block' ? 'pass' : '');
    const id = ToolCallId('mixed-failure-call');
    const args = failure === 'escaped-arguments' ? '{"pass\\u0077ord":"synthetic-stream-secret"}'
      : failure === 'malformed-arguments' ? 'password=synthetic-invalid-json' : '{"amount":37.50}';
    adapter.chunks = [
      { type: 'block-start', index: 0, blockType: 'text' },
      { type: 'text-delta', index: 0, text: first },
      { type: 'block-end', index: 0, block: { type: 'text', text: failure === 'mismatch' ? 'changed final text' : first } },
    ];
    if (failure === 'cross-block') adapter.chunks.push(
      { type: 'block-start', index: 1, blockType: 'text' },
      { type: 'text-delta', index: 1, text: 'word=synthetic-stream-secret' },
      { type: 'block-end', index: 1, block: { type: 'text', text: 'word=synthetic-stream-secret' } });
    if (failure === 'oversized') adapter.chunks.push({ type: 'block-start', index: 1, blockType: 'text' },
      { type: 'text-delta', index: 1, text: 'x'.repeat(800000) });
    if (failure === 'reasoning') adapter.chunks.push({ type: 'block-start', index: 1, blockType: 'reasoning' },
      { type: 'reasoning-delta', index: 1, text: 'unadmitted reasoning' });
    adapter.chunks.push(
      { type: 'block-start', index: 2, blockType: 'tool-call' },
      { type: 'tool-call-delta', index: 2, id, name: 'synthetic-tool', argumentsDelta: args },
      { type: 'block-end', index: 2, block: { type: 'tool-call', id, name: 'synthetic-tool', arguments: args } },
      { type: 'finish', reason: { kind: failure === 'truncated' ? 'max-tokens' : 'tool-calls' } });
    const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools' });
    const handle = await host.create('mixed-failure-' + failure);
    const visible: StreamChunk[] = [];
    host.ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk') visible.push(frame.chunk); });
    let effects = 0;
    adapter.afterChunk = async index => {
      if (index !== 1) return;
      if (failure === 'interrupted') throw new Error('synthetic provider interruption');
      if (failure === 'canceled') handle.agent.cancel({ kind: 'user' });
      if (failure === 'save-revoked') execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; c=Path(sys.argv[1]); pair_client(c,client_id='synthetic-dsh',entry='dsh',access_path=c.parent/'mcp-access-dsh.json',scopes=('recall',))", host.config.config], { cwd: checkout });
    };
    let fresh: Awaited<ReturnType<typeof installMemory>> | undefined;
    const reader = new MemoryClient(host.config); await reader.connect();
    try {
      host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic rejected stream',
        parameters: { amount: { type: 'number' } }, execute: async () => { effects++; adapter.chunks = undefined; return [{ type: 'text', text: 'must not run' }]; } }));
      handle.agent.followup(user('mixed-failure-input', '合成：失败前缀必须能回取，不执行工具。'));
      await handle.agent.whenIdle();
      assert.equal(effects, 0);
      assert.equal(visible.filter(value => value.type === 'text-delta').map(value => value.text).join(''), prefix);
      assert.equal(visible.some(value => value.type === 'tool-call-delta' || value.type === 'finish'), false);
      assert.doesNotMatch(JSON.stringify(visible), /synthetic-stream-secret|unadmitted reasoning/);
      assert.doesNotMatch(host.errors.map(String).join('\n'), /password|synthetic-invalid-json/);
      const found = await host.memory.streams(handle.agent.session.id);
      assert.equal(found.streams.length, 1);
      const saved = await reader.readStream(found.streams[0]);
      assert.equal(saved.text, prefix); assert.equal(saved.final_message_id, null);
      assert.equal(saved.state, failure === 'cross-block' ? 'protected' : failure === 'canceled' ? 'canceled' : failure === 'save-revoked' ? 'open' : 'failed');
      if (failure !== 'save-revoked') {
        await host.memory.close(); fresh = await installMemory(host.ctx, host.config);
        await fresh.rescan(handle.agent.session.id); await fresh.rescan(handle.agent.session.id);
        assert.deepEqual(await fresh.readStream(found.streams[0]), saved);
      }
      assert.deepEqual(await reader.readStream(found.streams[0]), saved);
    } finally { await fresh?.close(); await reader.close(); await handle.dispose(); await host.close(); }
  });
}

test('mixed durable stream accepts interleaved tool deltas and publishes parallel results in model order', { skip: !patchedPublication }, async () => {
  const adapter = new Synthetic();
  const first = ToolCallId('mixed-parallel-first'); const second = ToolCallId('mixed-parallel-second');
  adapter.chunks = [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id: first, name: 'first', argumentsDelta: '{"amount":' },
    { type: 'block-start', index: 1, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 1, id: second, name: 'second', argumentsDelta: '{"amount":48.75}' },
    { type: 'block-end', index: 1, block: { type: 'tool-call', id: second, name: 'second', arguments: '{"amount":48.75}' } },
    { type: 'tool-call-delta', index: 0, id: first, argumentsDelta: '37.50}' },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id: first, name: 'first', arguments: '{"amount":37.50}' } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ];
  const host = await boot(adapter, false, false, { toolPublication: true, streamMode: 'durable_tools', maxParallelToolCalls: 2 });
  const handle = await host.create('mixed-parallel');
  const secondDone = Promise.withResolvers<void>();
  const bodies: string[] = [];
  try {
    for (const name of ['first', 'second']) host.ctx.tools.register({ ...defineContentToolFixture({ name,
      description: 'Synthetic parallel mixed stream', parameters: { amount: { type: 'number' } }, execute: async () => {
        if (name === 'first') await secondDone.promise;
        bodies.push(name); if (name === 'second') secondDone.resolve(); adapter.chunks = undefined;
        return [{ type: 'text', text: name + '合成结果。' }];
      } }), isConcurrencySafe: () => true });
    handle.agent.followup(user('mixed-parallel-input', '合成：交错参数与并行执行。'));
    await handle.agent.whenIdle();
    assert.deepEqual(host.errors, []); assert.deepEqual(bodies, ['second', 'first']);
    assert.deepEqual(handle.agent.session.snapshotEvents().filter(value => value.type === 'tool/result').map(value => value.data.message.toolCallId), [first, second]);
  } finally { secondDone.resolve(); await handle.dispose(); await host.close(); }
});

test('trusted recovery creates one durable read-only continuation without replaying the failed tool',
    { skip: Reflect.get(ToolRuntime, 'evernookRecoveryVersion') !== 1 }, async () => {
  const adapter = new Synthetic(); adapter.toolOnce = true;
  const host = await boot(adapter, false, false, { toolPublication: true });
  const source = await host.create('recovery-source');
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; c=Path(sys.argv[1]); pair_client(c,client_id='synthetic-owner',entry='owner',access_path=Path(sys.argv[2]))", host.config.config, owner], { cwd: checkout });
  let failCompletion = true;
  const control = (input: Record<string, unknown>) => {
    if (input.action === 'complete' && failCompletion) { failCompletion = false; return Promise.reject(new Error('synthetic completion receipt lost')); }
    return runLocalCli(host.config, owner, 'host-recovery', input);
  };
  const recovery = await createRecovery(host.ctx, host.config, control);
  let effects = 0;
  let fresh: Awaited<ReturnType<typeof createRecovery>> | undefined;
  let resumed: Awaited<ReturnType<typeof host.ctx.agents.resume>> | undefined;
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic recovery side effect',
      parameters: { amount: { type: 'number' } }, execute: async () => { effects++; return [{ type: 'text', text: '合成副作用已经发生一次。' }]; } }));
    host.ctx.on('evernook/tools/before-publish', async exec => {
      if (exec.agent?.session.id === source.agent.session.id) throw new Error('synthetic lost publication after a real capture');
    });
    source.agent.followup(user('recovery-input', '合成任务：操作一次，然后核对证据。'));
    await source.agent.whenIdle();
    assert.equal(effects, 1); assert.equal(adapter.requests.length, 1);
    const original = source.agent.session.snapshotEvents();
    const view = await recovery.read(String(source.agent.session.id));
    assert.equal(view.blocked, true); assert.equal(view.calls[0].result, 'complete');
    assert.ok(view.evidence.some(value => value.phase === 'input'));
    const preview = await recovery.preview(String(source.agent.session.id));
    await assert.rejects(recovery.confirm(view.session_id, preview.proposal_id, view.fingerprint), /synthetic completion receipt lost/);
    const persisted = await host.ctx.sessionPersistence.open(SessionId(preview.target_session), 'read');
    try { assert.ok((await persisted.read()).events.some(value => pluginRecordOf(value)?.type === 'plugin:dsh-evernook/recovery-readonly')); }
    finally { await persisted.close(); }
    const results = await Promise.all([0, 1].map(() => recovery.confirm(view.session_id, String(preview.proposal_id), view.fingerprint)));
    assert.deepEqual(results[0], results[1]); assert.equal(results[0].state, 'complete');
    assert.equal(effects, 1); assert.equal(adapter.requests.length, 1);
    assert.deepEqual(source.agent.session.snapshotEvents(), original);
    await recovery.close(); fresh = await createRecovery(host.ctx, host.config, control);
    const repeated = await fresh.preview(view.session_id);
    assert.equal((await fresh.confirm(view.session_id, String(repeated.proposal_id), view.fingerprint)).target_session, results[0].target_session);
    const ownedTarget = await host.ctx.sessionPersistence.open(SessionId(results[0].target_session), 'write');
    try {
      assert.equal((await fresh.confirm(view.session_id, preview.proposal_id, view.fingerprint)).target_session, results[0].target_session,
        'Reading a completed recovery receipt must not compete for the target writer lease');
    } finally { await ownedTarget.close(); }
    resumed = await host.ctx.agents.resume({ resumeSessionId: SessionId(results[0].target_session), agentOptions: { provider: 'synthetic', model: 'local-test' } });
    assert.equal(adapter.requests.length, 1, 'Creating and resuming an empty recovery never invokes the provider');
    resumed.agent.followup(user('recovery-discussion', '只讨论已经保存的证据，不做任何操作。'));
    await resumed.agent.whenIdle();
    assert.equal(adapter.requests.length, 2);
    assert.match(JSON.stringify(adapter.requests.at(-1)), /untrusted_read_only_recovery/);
    assert.match(JSON.stringify(adapter.requests.at(-1)), /合成副作用已经发生一次/);
    assert.equal(effects, 1);
    await host.memory.close();
    const denied = await host.ctx.tools.execute({ callId: ToolCallId('different-identity-after-unload'),
      name: 'synthetic-tool', arguments: { amount: 37.5 }, agent: resumed.agent, signal: AbortSignal.timeout(5000) });
    assert.equal(denied.isError, true); assert.equal(denied.error?.info?.code, 'EVERNOOK_RECOVERY_READ_ONLY');
    assert.equal(effects, 1);
  } finally { await resumed?.dispose(); await fresh?.close(); await recovery.close(); await source.dispose(); await host.close(); }
});

for (const failure of ['protected', 'stale', 'canceled', 'active', 'counter-suppressed', 'missing-target', 'invalid-target']) test(`trusted recovery handles ${failure} without replaying side effects`,
    { skip: Reflect.get(ToolRuntime, 'evernookRecoveryVersion') !== 1 }, async () => {
  const adapter = new Synthetic(); adapter.toolOnce = true;
  const host = await boot(adapter, failure === 'counter-suppressed', false, { toolPublication: true });
  const source = await host.create('recovery-' + failure);
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; c=Path(sys.argv[1]); pair_client(c,client_id='synthetic-owner',entry='owner',access_path=Path(sys.argv[2]))", host.config.config, owner], { cwd: checkout });
  const recovery = await createRecovery(host.ctx, host.config, input => runLocalCli(host.config, owner, 'host-recovery', input));
  const started = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>();
  let effects = 0;
  let resumed: Awaited<ReturnType<typeof host.ctx.agents.resume>> | undefined;
  function suppress(id: string) {
    execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import MemoryRuntime,Event,Source,Change; r=MemoryRuntime(Path(sys.argv[1])); a=r.access_from_file(Path(sys.argv[2])); c=r.submit(a,Event(Source('owner','synthetic-review','main','confirmation'),'user',1,None,'complete','合成确认停止召回结果')); r.change(a,Change('suppress-reviewed-result',sys.argv[3],1,'suppress',c.message_id,'synthetic confirmation'))", host.config.config, owner, id], { cwd: checkout });
  }
  try {
    host.ctx.tools.register(defineContentToolFixture({ name: 'synthetic-tool', description: 'Synthetic recovery failure cases',
      parameters: { amount: { type: 'number' } }, execute: async () => {
        effects++; started.resolve(); if (failure === 'active') await release.promise;
        return [{ type: 'text', text: failure === 'protected' ? 'password=synthetic-unpublished-recovery-secret' : '合成待核对结果。' }];
      } }));
    host.ctx.on('evernook/tools/before-publish', async exec => {
      if (exec.agent?.session.id === source.agent.session.id) throw new Error('synthetic blocked publication');
    });
    source.agent.followup(user('recovery-failure-input', '合成：只执行一次。'));
    if (failure === 'active') {
      await started.promise;
      await assert.rejects(recovery.read(String(source.agent.session.id)), /evernook:recovery_busy/);
      release.resolve();
    }
    await source.agent.whenIdle();
    const view = await recovery.read(String(source.agent.session.id));
    assert.doesNotMatch(JSON.stringify(view), /synthetic-unpublished-recovery-secret/);
    if (failure === 'protected') assert.equal(view.calls[0].result, 'unconfirmed');
    const preview = await recovery.preview(view.session_id);
    if (failure === 'stale') suppress(view.evidence.find(item => item.phase === 'result')!.message_id);
    if (failure === 'canceled') await recovery.cancel(preview.proposal_id);
    if (failure === 'stale' || failure === 'canceled') {
      await assert.rejects(recovery.confirm(view.session_id, preview.proposal_id, view.fingerprint),
        failure === 'stale' ? /evernook:recovery_stale/ : /evernook:recovery_canceled/);
      await assert.rejects(host.ctx.sessionPersistence.open(SessionId(preview.target_session), 'read'));
    } else {
      const result = await recovery.confirm(view.session_id, preview.proposal_id, view.fingerprint);
      if (failure === 'missing-target' || failure === 'invalid-target') {
        const files = (await readdir(join(host.root, 'sessions'), { recursive: true }))
          .filter(path => path.split(/[\\/]/).includes(result.target_session) && path.endsWith('session.v4.jsonl'));
        assert.equal(files.length, 1);
        const file = join(host.root, 'sessions', files[0]);
        const original = await readFile(file);
        try {
          if (failure === 'missing-target') await rename(file, file + '.withheld');
          else {
            const rows = original.toString('utf8').trimEnd().split('\n').map(line => JSON.parse(line));
            const marker = rows.find(row => row.type === 'plugin:dsh-evernook/recovery-readonly'); assert.ok(marker);
            marker.data.fingerprint = '0'.repeat(64);
            await writeFile(file, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
          }
          await assert.rejects(recovery.confirm(view.session_id, preview.proposal_id, view.fingerprint),
            failure === 'missing-target' ? /recovery_target_unavailable/ : /invalid_recovery_target/);
        } finally {
          if (failure === 'missing-target') await rename(file + '.withheld', file);
          else await writeFile(file, original);
        }
      }
      assert.equal(result.read_only, true);
      if (failure === 'counter-suppressed') {
        const counter = host.config.memoryBudget!.countRequest;
        let changed = false;
        host.config.memoryBudget!.countRequest = request => {
          if (!changed && JSON.stringify(request).includes('untrusted_read_only_recovery')) {
            changed = true; suppress(view.evidence.find(item => item.phase === 'result')!.message_id);
          }
          return counter(request);
        };
        resumed = await host.ctx.agents.resume({ resumeSessionId: SessionId(result.target_session), agentOptions: { provider: 'synthetic', model: 'local-test' } });
        resumed.agent.followup(user('review-counter-input', '只读讨论证据。'));
        await resumed.agent.whenIdle();
        assert.equal(changed, true);
      }
    }
    assert.equal(adapter.requests.length, 1);
    assert.equal(effects, 1);
  } finally { release.resolve(); await resumed?.dispose(); await recovery.close(); await source.dispose(); await host.close(); }
});

test('independent service carries real dsh save recall search and survives adapter closure', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, true, true, { service: true });
  t.after(() => host.close());
  const reader = new MemoryClient(host.config); await reader.connect();
  t.after(() => reader.close());
  const first = await host.create('service-first'); t.after(() => first.dispose());
  first.agent.followup(user('service-one', 'hello world'));
  await first.agent.whenIdle();
  const earlier = host.memory.status(SessionId('service-first')).latest_input!;
  assert.equal((await reader.read(earlier.message_id)).event.text, 'hello world');
  const found = await reader.call<{ hits: { message_id: string }[] }>('search',
    { query: 'hello world', mode: 'exact' });
  assert.ok(found.hits.some(hit => hit.message_id === earlier.message_id));
  const second = await host.create('service-second'); t.after(() => second.dispose());
  second.agent.followup(user('service-two', 'hello world'));
  await second.agent.whenIdle();
  const request = adapter.requests.at(-1)!;
  assert.ok(JSON.stringify(request).includes(earlier.message_id));
  assert.deepEqual(host.errors, []);
  await host.memory.close();
  const status = await reader.call<{ service: { independent: boolean } }>('status', {});
  assert.equal(status.service.independent, true);
  assert.equal((await reader.read(earlier.message_id)).event.text, 'hello world');
});

test('web service page corrects and suppresses evidence used by fresh native dsh sessions', async t => {
  const adapter = new Synthetic();
  const host = await boot(adapter, false, true, { service: true });
  t.after(() => host.close());
  await host.memory.close();
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  host.ctx.provide('connection', { fetch: { register: () => () => {} } });
  const web = await installWeb(host.ctx, { python: python!, config: host.config.config,
    access: host.config.access, maintenanceAccess: owner, entry: 'dsh', streamMode: 'durable_text' });
  t.after(() => web.close());
  const reader = new MemoryClient({ ...host.config, serviceRequired: true }); await reader.connect();
  t.after(() => reader.close());
  const first = await host.create('web-service-first'); t.after(() => first.dispose());
  first.agent.followup(user('web-service-one', 'hello original world')); await first.agent.whenIdle();
  const originalPage = await reader.call<{ hits: { message_id: string; role: string }[] }>('search',
    { query: 'hello original world', mode: 'exact' });
  const original = { hits: originalPage.hits.filter(hit => hit.role === 'user') };
  assert.equal(original.hits.length, 1);
  let csrf = '';
  async function page(input: Record<string, unknown>) {
    const response = await web.handle(new Request('http://dsh.internal/api/evernook/manage', {
      method: 'POST', headers: { host: '127.0.0.1:37849', origin: 'http://127.0.0.1:37849',
        'content-type': 'application/json', ...(csrf ? { 'x-evernook-csrf': csrf } : {}) },
      body: JSON.stringify(input) }));
    const body = await response.json() as Record<string, any>;
    assert.equal(response.status, 200, JSON.stringify(body));
    return body;
  }
  const bootstrap = await page({ action: 'bootstrap' }); csrf = bootstrap.csrf;
  assert.equal(bootstrap.private_capture, false);
  const forbiddenOpen = await web.handle(new Request('http://dsh.internal/api/evernook/manage', {
    method: 'POST', headers: { host: '127.0.0.1:37849', origin: 'http://127.0.0.1:37849',
      'content-type': 'application/json', 'x-evernook-csrf': csrf }, body: JSON.stringify({ action: 'private_open' }) }));
  assert.equal(forbiddenOpen.status, 400);
  async function confirm(operation: Record<string, unknown>) {
    const preview = await page({ action: 'preview', operation });
    return page({ action: 'confirm', proposal_id: preview.result.proposal_id, operation });
  }
  await confirm({ kind: 'source_correct', message_id: original.hits[0].message_id, text: 'hello changed world' });
  const corrected = await reader.call<{ hits: { message_id: string }[] }>('search',
    { query: 'hello changed world', mode: 'exact' });
  assert.equal(corrected.hits.length, 1);
  const second = await host.create('web-service-second'); t.after(() => second.dispose());
  second.agent.followup(user('web-service-two', 'hello changed world')); await second.agent.whenIdle();
  assert.ok(JSON.stringify(adapter.requests.at(-1)).includes(corrected.hits[0].message_id));
  await confirm({ kind: 'source_suppress', message_id: corrected.hits[0].message_id });
  const third = await host.create('web-service-third'); t.after(() => third.dispose());
  third.agent.followup(user('web-service-three', 'hello changed world')); await third.agent.whenIdle();
  assert.ok(!JSON.stringify(adapter.requests.at(-1)).includes(corrected.hits[0].message_id));
  assert.deepEqual(host.errors, []);
});

test('web service startup rejects an ordinary identity missing capture', async t => {
  const host = await boot(new Synthetic(), false, false, { service: true });
  t.after(() => host.close());
  await host.memory.close();
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  execFileSync(python!, ['-B', '-m', 'evernook.cli', 'service-pair', '--config', host.config.config,
    '--access', owner, '--client-id', 'synthetic-dsh', '--entry', 'dsh', '--scopes', 'recall'], { cwd: checkout });
  await assert.rejects(installWeb(host.ctx, { python: python!, config: host.config.config,
    access: host.config.access, maintenanceAccess: owner, entry: 'dsh', streamMode: 'durable_text' }),
    /invalid_maintenance_identity/);
});
