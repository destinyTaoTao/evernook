import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { Type, fauxAssistantMessage, fauxProvider, fauxToolCall, type TranscriptContext } from '@earendil-works/pi-ai';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager,
  type ExtensionContext, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { installPiMemory } from '../src/pi.ts';
import { Context as DshContext } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import DshLlm, { LlmAdapter, MessageId, freezeMessage, type StreamChunk } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import { installMemory } from '../src/dsh.ts';

const python = process.env.EVERNOOK_TEST_PYTHON;
const output = process.env.EVERNOOK_TEST_OUTPUT;
if (!python || !output) throw new Error('Set external synthetic EVERNOOK_TEST_PYTHON and EVERNOOK_TEST_OUTPUT');
const checkout = resolve(import.meta.dirname, '../../..');

async function boot(reply = '合成 pi 回复🙂\r\n37.50元。', tools: readonly ToolDefinition[] = [], systemHistory?: string) {
  await mkdir(output!, { recursive: true });
  const root = await mkdtemp(join(output!, 'pi-native-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; c,a=initialize(Path(sys.argv[1]),client_id='synthetic-pi',entry='pi'); p=c.parent/'mcp-access-pi.json'; pair_client(c,client_id=a.client_id,entry='pi',access_path=p); print(json.dumps({'config':str(c),'access':str(p)}))";
  const paths = JSON.parse(execFileSync(python!, ['-c', script, join(root, 'memory')], { cwd: checkout, encoding: 'utf8' }));
  const faux = fauxProvider({ provider: 'synthetic-local-pi', tokenSize: { min: 2, max: 3 } });
  const requests: TranscriptContext[] = [];
  let inspect: ((signal?: AbortSignal) => void | Promise<void>) | undefined;
  let rawObserver: (() => void) | undefined;
  faux.setResponses(Array.from({ length: 8 }, () => async (context, options, _state, model) => {
    requests.push(structuredClone(context));
    await inspect?.(options?.signal);
    await options?.onProviderStreamEvent?.({ kind: 'synthetic-raw', text: reply }, model);
    await options?.onProviderStreamEvent?.({ kind: 'synthetic-raw-end' }, model);
    if (tools.length && requests.length === 1) return fauxAssistantMessage(fauxToolCall(tools[0].name, { amount: 37.5 }), { stopReason: 'toolUse' });
    return fauxAssistantMessage(reply);
  }));
  const runtime = await ModelRuntime.create({ authPath: join(root, 'pi-auth.json'), modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false });
  runtime.registerNativeProvider(faux.provider);
  const settings = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
  let memory: Awaited<ReturnType<typeof installPiMemory>> | undefined;
  let context: ExtensionContext | undefined;
  const rawChecks: boolean[] = [];
  function rows() {
    const db = new DatabaseSync(join(root, 'memory/memory.sqlite3'), { readOnly: true });
    try { return db.prepare('SELECT event,receipt FROM messages').all().map(row => ({ event: JSON.parse(String(row.event)), receipt: JSON.parse(String(row.receipt)) })); }
    finally { db.close(); }
  }
  const resources = new DefaultResourceLoader({ cwd: root, agentDir: join(root, 'pi-agent'), settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [async pi => {
      memory = await installPiMemory(pi, { python: python!, config: paths.config, access: paths.access, entry: 'pi',
        cwd: checkout, target: faux.getModel(), tools });
      pi.on('context', (event, ctx) => {
        context = ctx;
        if (systemHistory && JSON.stringify(event.messages).includes('safe-probe')) return { messages: [...event.messages,
          { role: 'system' as const, content: 'fixed', sections: { historical: systemHistory }, timestamp: Date.now() }] };
      });
      pi.on('provider_stream_event', () => {
        rawChecks.push(rows().some(row => row.event.role === 'assistant' && row.event.text === reply && row.receipt.original_saved));
        rawObserver?.();
      });
    }] });
  await resources.reload();
  const model = { ...faux.getModel(), provider: 'evernook-memory', api: 'evernook-buffered-pi-v1' };
  const { session } = await createAgentSession({ cwd: root, agentDir: join(root, 'pi-agent'), modelRuntime: runtime,
    resourceLoader: resources, settingsManager: settings, noTools: tools.length ? 'builtin' : 'all',
    model, sessionManager: SessionManager.create(root, join(root, 'pi-sessions')) });
  return { root, paths, faux, rows, requests, rawChecks, session,
    setInspect(value: (signal?: AbortSignal) => void | Promise<void>) { inspect = value; },
    setRawObserver(value: () => void) { rawObserver = value; },
    async rescan() { assert.ok(memory && context); await memory.rescan(context); },
    async close() { await memory?.close(); session.dispose(); },
  };
}

test('real pi SDK saves native user before target provider and every published text/raw observer sees saved assistant', async () => {
  const host = await boot();
  const failures: string[] = [];
  try {
    host.setInspect(() => assert.ok(host.rows().some(row => row.event.role === 'user' && row.event.text === '合成：预算37元。')));
    host.session.subscribe(event => {
      if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') {
        if (!host.rows().some(row => row.event.role === 'assistant' && row.event.text === '合成 pi 回复🙂\r\n37.50元。' && row.receipt.original_saved)) failures.push('unsaved text publication');
      }
    });
    await host.session.prompt('合成：预算37元。');
    assert.equal(host.requests.length, 1);
    assert.deepEqual(failures, []);
    assert.ok(host.rawChecks.length > 0);
    assert.ok(host.rawChecks.every(Boolean));
    await host.rescan();
    const count = host.rows().length;
    await host.rescan();
    assert.equal(host.rows().length, count);
    await host.session.prompt('预算');
    assert.equal(host.requests.length, 2);
    const data = host.requests[1].messages.filter(message => message.role === 'user');
    assert.match(JSON.stringify(data), /untrusted_memory_evidence/);
    assert.match(JSON.stringify(data), /预算37元/);
    const system = host.requests[1].messages.filter(message => message.role === 'system');
    assert.doesNotMatch(JSON.stringify(system), /untrusted_memory_evidence/);
  } finally { await host.close(); }
});

test('real pi provider wrapper blocks protected assistant output before live text and parsed-provider observers', async () => {
  const host = await boot('password=synthetic-pi-protected-sentinel-123');
  const displayed: string[] = [];
  try {
    host.session.subscribe(event => { if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') displayed.push(event.assistantMessageEvent.delta); });
    await host.session.prompt('合成：受保护输出路径');
    assert.equal(host.requests.length, 1);
    assert.deepEqual(displayed, []);
    assert.deepEqual(host.rawChecks, []);
    const paths = await readdir(join(host.root, 'memory'));
    const bytes = Buffer.concat(await Promise.all(paths.filter(name => name.startsWith('memory.sqlite3')).map(name => readFile(join(host.root, 'memory', name)))));
    assert.ok(!bytes.includes(Buffer.from('synthetic-pi-protected-sentinel-123')));
  } finally { await host.close(); }
});

test('real pi input storage failure prevents target provider even when extension exceptions normally are swallowed', async () => {
  const host = await boot();
  try {
    const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'));
    db.exec("CREATE TRIGGER synthetic_pi_failure BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'synthetic failure'); END");
    db.close();
    await host.session.prompt('合成：不能假装保存成功').catch(() => {});
    assert.equal(host.requests.length, 0);
    assert.equal(host.rows().length, 0);
  } finally { await host.close(); }
});

test('pi resumes safe input after protected history and a full rescan without dispatching the raw original', async () => {
  const host = await boot();
  try {
    await host.session.prompt('password=synthetic-history-pi-123').catch(() => {});
    assert.equal(host.requests.length, 0);
    assert.ok(host.rows().some(row => row.event.role === 'user' && !row.receipt.original_saved));
    await host.session.prompt('合成：后续安全问题');
    assert.equal(host.requests.length, 1);
    assert.doesNotMatch(JSON.stringify(host.requests[0]), /synthetic-history-pi-123/);
    assert.doesNotMatch(JSON.stringify(host.rows()), /synthetic-history-pi-123/);
    await host.session.reload();
    await host.session.prompt('合成：重启后继续安全交流');
    assert.equal(host.requests.length, 2);
    assert.doesNotMatch(JSON.stringify(host.requests[1]), /synthetic-history-pi-123/);
    await host.rescan();
    await host.session.prompt('合成：重扫后继续安全交流');
    assert.equal(host.requests.length, 3);
    assert.doesNotMatch(JSON.stringify(host.requests[2]), /synthetic-history-pi-123/);
  } finally { await host.close(); }
});

test('pi final dispatch rejects protected history reintroduced in provider-visible system sections', async () => {
  const secret = 'password=synthetic-system-history-123';
  const host = await boot('合成安全回复', [], secret);
  try {
    await host.session.prompt(secret).catch(() => {});
    await host.session.prompt('safe-probe').catch(() => {});
    assert.equal(host.requests.length, 0);
  } finally { await host.close(); }
});

test('real pi wrapped tool buffers native partial updates until input and complete result are durable', async () => {
  let executed = 0;
  let host: Awaited<ReturnType<typeof boot>>;
  const tool: ToolDefinition = { name: 'synthetic_tool', label: 'Synthetic tool', description: 'Local synthetic fixture',
    parameters: Type.Object({ amount: Type.Number() }),
    async execute(_id, _input, _signal, onUpdate) {
      executed++;
      assert.ok(host.rows().some(row => row.event.role === 'tool' && row.event.text.includes('37.5')));
      onUpdate?.({ content: [{ type: 'text', text: '合成部分结果37.50' }], details: {} });
      return { content: [{ type: 'text', text: '合成完整工具结果🙂\r\n37.50元。' }], details: { synthetic: true } };
    } };
  host = await boot('合成工具后的回复', [tool]);
  const checks: boolean[] = [];
  try {
    host.session.subscribe(event => {
      if (event.type === 'tool_execution_update' || event.type === 'tool_execution_end') checks.push(
        host.rows().some(row => row.event.role === 'tool' && row.event.text === '合成完整工具结果🙂\r\n37.50元。' && row.receipt.original_saved));
    });
    await host.session.prompt('合成：运行已登记的合成工具');
    assert.equal(executed, 1);
    assert.equal(host.requests.length, 2);
    assert.ok(checks.length > 0);
    assert.ok(checks.every(Boolean));
  } finally { await host.close(); }
});

test('real pi split sensitive tool prefixes stay out of native partial updates and ordinary storage', async () => {
  const tool: ToolDefinition = { name: 'synthetic_tool', label: 'Synthetic tool', description: 'Local synthetic fixture',
    parameters: Type.Object({ amount: Type.Number() }),
    async execute(_id, _input, _signal, onUpdate) {
      onUpdate?.({ content: [{ type: 'text', text: 'pass' }], details: {} });
      onUpdate?.({ content: [{ type: 'text', text: 'word=synthetic-pi-tool-sentinel-123' }], details: {} });
      return { content: [{ type: 'text', text: '合成终态没有凭证' }], details: {} };
    } };
  const host = await boot('合成：工具失败已明确', [tool]);
  const partials: unknown[] = [];
  try {
    host.session.subscribe(event => { if (event.type === 'tool_execution_update') partials.push(event.partialResult); });
    await host.session.prompt('合成：检查敏感工具结果');
    assert.deepEqual(partials, []);
    const paths = await readdir(join(host.root, 'memory'));
    const bytes = Buffer.concat(await Promise.all(paths.filter(name => name.startsWith('memory.sqlite3')).map(name => readFile(join(host.root, 'memory', name)))));
    assert.ok(!bytes.includes(Buffer.from('synthetic-pi-tool-sentinel-123')));
    assert.ok(host.rows().some(row => row.event.text.includes('tool_attempt_unpublished')));
  } finally { await host.close(); }
});

test('actual dsh and pi kernels with separately paired entries recall from the same authoritative ledger', async () => {
  const host = await boot();
  const script = "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-cross-dsh',entry='dsh',access_path=Path(sys.argv[2]))";
  const dshAccess = join(host.root, 'memory/mcp-access-cross-dsh.json');
  execFileSync(python!, ['-c', script, host.paths.config, dshAccess], { cwd: checkout });
  const ctx = new DshContext();
  let memory: Awaited<ReturnType<typeof installMemory>> | undefined;
  let agent: Awaited<ReturnType<typeof ctx.agents.create>> | undefined;
  try {
    await ctx.plugin(SessionStore).await();
    await ctx.plugin(SessionProjectionRegistry).await();
    await ctx.plugin(AgentRegistry).await();
    await ctx.plugin(DshLlm).await();
    await ctx.plugin(LocalAttachmentStore, { dshHome: join(host.root, 'dsh-home') }).await();
    await ctx.plugin(ToolRuntime).await();
    await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
    await ctx.plugin(persistence, { root: join(host.root, 'dsh-sessions'), compression: 'none' }).await();
    await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 }).await();
    class SyntheticDsh extends LlmAdapter {
      async *stream(): AsyncIterable<StreamChunk> {
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text: '合成dsh回复。' };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: '合成dsh回复。' } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    ctx.llm.registerAdapter(['synthetic-cross'], new SyntheticDsh());
    memory = await installMemory(ctx, { python: python!, config: host.paths.config, access: dshAccess, entry: 'dsh', cwd: checkout });
    agent = await ctx.agents.create({ sessionId: SessionId('synthetic-cross'), meta: { cwd: host.root, delegationDepth: 0 },
      agentOptions: { provider: 'synthetic-cross', model: 'offline' } });
    agent.agent.followup(freezeMessage({ id: MessageId('synthetic-cross-user'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '合成：在dsh记下喜欢乌龙茶，预算37元。' }] }));
    await agent.agent.whenIdle();
    await memory.capture(agent.agent.session);
    await host.session.prompt('乌龙茶');
    assert.equal(host.requests.length, 1);
    assert.ok(host.rows().some(row => row.event.role === 'user' && row.event.source.entry === 'dsh' && row.event.text.includes('预算37元')));
    assert.ok(host.rows().some(row => row.event.role === 'user' && row.event.source.entry === 'pi' && row.event.text === '乌龙茶'));
    const injected = host.requests[0].messages.filter(message => message.role === 'user');
    assert.match(JSON.stringify(injected), /在dsh记下喜欢乌龙茶，预算37元/);
  } finally { await agent?.dispose(); await memory?.close(); await ctx.fiber.dispose(); await host.close(); }
});

test('actual pi abort cancels delegated generation and never releases unpublished text or raw provider observations', async () => {
  const host = await boot();
  let started!: () => void;
  const checkpoint = new Promise<void>(resolve => { started = resolve; });
  const displayed: string[] = [];
  host.setInspect(async signal => {
    assert.ok(signal);
    started();
    if (!signal.aborted) await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
  });
  const pending = host.session.prompt('合成：取消正在进行的模型请求');
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new Error('synthetic provider checkpoint not reached')), 15000); });
  try {
    host.session.subscribe(event => { if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') displayed.push(event.assistantMessageEvent.delta); });
    await Promise.race([checkpoint, deadline, pending.then(() => { throw new Error('request ended before synthetic provider checkpoint'); })]);
    await host.session.abort();
    await pending.catch(() => {});
    assert.deepEqual(displayed, []);
    assert.deepEqual(host.rawChecks, []);
    assert.ok(host.rows().some(row => row.event.text.includes('assistant_attempt_unpublished')));
  } finally { if (timer) clearTimeout(timer); await host.session.abort(); await pending.catch(() => {}); await host.close(); }
});

test('actual pi cancellation during durable raw replay stops the remaining observers and all text publication', async () => {
  const host = await boot();
  const displayed: string[] = [];
  let cancel: Promise<void> | undefined;
  try {
    host.setRawObserver(() => { cancel = host.session.abort(); });
    host.session.subscribe(event => { if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') displayed.push(event.assistantMessageEvent.delta); });
    await host.session.prompt('合成：在已保存观察事件回放期间取消');
    await cancel;
    assert.deepEqual(host.rawChecks, [true]);
    assert.deepEqual(displayed, []);
  } finally { await host.session.abort(); await host.close(); }
});
