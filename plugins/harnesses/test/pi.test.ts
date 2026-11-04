import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { Type, fauxAssistantMessage, fauxProvider, fauxToolCall, type TranscriptContext } from '@earendil-works/pi-ai';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager,
  type ExtensionContext, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { installPiMemory } from '../src/pi.ts';
import type { PiConfig } from '../src/pi.ts';
import { tokenizerFixture } from './tokenizer-fixture.ts';
import { Context as DshContext } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import DshLlm, { LlmAdapter, MessageId, freezeMessage, type StreamChunk, type GenerateOptions } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import { installMemory } from '../src/dsh.ts';
import { MemoryClient } from '../src/client.ts';

const python = process.env.EVERNOOK_TEST_PYTHON;
const output = process.env.EVERNOOK_TEST_OUTPUT;
if (!python || !output) throw new Error('Set external synthetic EVERNOOK_TEST_PYTHON and EVERNOOK_TEST_OUTPUT');
const checkout = resolve(import.meta.dirname, '../../..');

async function boot(reply = '合成 pi 回复🙂\r\n37.50元。', tools: readonly ToolDefinition[] = [], systemHistory?: string, budgeted = false, contextImage = false, maintained = false, durable = false, lifecycle: { consolidation?: boolean; compaction?: boolean } = {}) {
  await mkdir(output!, { recursive: true });
  const root = await mkdtemp(join(output!, 'pi-native-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client; c,a=initialize(Path(sys.argv[1]),client_id='synthetic-owner',entry='owner'); p=c.parent/'mcp-access-pi.json'; pair_client(c,client_id='synthetic-pi',entry='pi',access_path=p,scopes=('capture','recall')); print(json.dumps({'config':str(c),'access':str(p)}))";
  const paths = JSON.parse(execFileSync(python!, ['-c', script, join(root, 'memory')], { cwd: checkout, encoding: 'utf8' }));
  const faux = fauxProvider({ provider: 'synthetic-local-pi', tokenSize: { min: 2, max: 3 },
    ...(durable ? { tokensPerSecond: 40 } : {}) });
  const fixture = budgeted ? await tokenizerFixture(root, faux.getModel().id) : undefined;
  const memoryConfig: PiConfig = { python: python!, config: paths.config, access: paths.access, entry: 'pi',
    cwd: checkout, target: faux.getModel(), tools };
  if (durable) Object.assign(memoryConfig, { streamMode: 'durable_text' });
  if (lifecycle.consolidation) {
    const access = join(root, 'memory/mcp-access-consolidation.json');
    execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime; from evernook.scheduler import ConsolidationScheduler; c=Path(sys.argv[1]); owner=c.parent/'mcp-access-owner.json'; a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=owner); pair_client(c,client_id='synthetic-consolidation',entry='pi-background',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept')); ConsolidationScheduler(MemoryRuntime(c)).configure(a,paused=True,min_pending=1,idle_seconds=0,max_sources_per_run=4,max_runs_per_trigger=2,max_trigger_seconds=2)", paths.config, access], { cwd: checkout });
    memoryConfig.consolidation = { access, pollMs: 100 };
  }
  const reviews: import('../src/maintenance.ts').MaintenanceReview[] = [];
  const outcomes: import('../src/maintenance.ts').MaintenanceOutcome[] = [];
  let onReview: (() => void | Promise<void>) | undefined;
  if (maintained) {
    const access = join(root, 'memory/mcp-access-maintenance.json');
    execFileSync(python!, ['-c', "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-maintenance',entry='pi-maintenance',access_path=Path(sys.argv[2]),scopes=('recall','maintain','accept','admin'))", paths.config, access], { cwd: checkout });
    memoryConfig.maintenance = { access, onReview: async review => { reviews.push(review); await onReview?.(); }, onResult: async result => { outcomes.push(result); } };
  }
  if (fixture) {
    memoryConfig.contextConfig = fixture.contextConfig;
    memoryConfig.memoryBudget = { provider: faux.getModel().provider, modelId: faux.getModel().id,
      memoryTokens: 512, contextWindow: 20000, outputReserveTokens: 1000,
      countRequest: request => fixture.target.encode(JSON.stringify(request.context), { add_special_tokens: false }).ids.length };
  }
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
  const settings = SettingsManager.inMemory({ compaction: { enabled: false, ...(lifecycle.compaction ? { keepRecentTokens: 64, reserveTokens: 1000 } : {}) }, retry: { enabled: false } });
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
      memory = await installPiMemory(pi, memoryConfig);
      pi.on('session_start', (_event, ctx) => { context = ctx; });
      pi.on('context', (event, ctx) => {
        context = ctx;
        if (contextImage) return { messages: [...event.messages, { role: 'user' as const,
          content: [{ type: 'image' as const, data: 'AA==', mimeType: 'image/png' }], timestamp: Date.now() }] };
        if (systemHistory && JSON.stringify(event.messages).includes('safe-probe')) return { messages: [...event.messages,
          { role: 'system' as const, content: 'fixed', sections: { historical: systemHistory }, timestamp: Date.now() }] };
      });
      pi.on('provider_stream_event', () => {
        rawChecks.push(rows().some(row => row.event.role === 'assistant' && row.event.text === reply && row.receipt.original_saved));
        rawObserver?.();
      });
    }] });
  await resources.reload();
  assert.deepEqual(resources.getExtensions().errors, []);
  const model = { ...faux.getModel(), provider: 'evernook-memory', api: durable ? 'evernook-durable-pi-v1' : 'evernook-buffered-pi-v1' };
  const openSession = (manager: SessionManager) => createAgentSession({ cwd: root, agentDir: join(root, 'pi-agent'), modelRuntime: runtime,
    resourceLoader: resources, settingsManager: settings, noTools: tools.length || maintained ? 'builtin' : 'all',
    model, sessionManager: manager });
  let { session } = await openSession(SessionManager.create(root, join(root, 'pi-sessions')));
  if (lifecycle.compaction) await session.bindExtensions({ onError() {} });
  return { root, paths, faux, rows, requests, rawChecks, get session() { return session; }, memoryConfig, fixture,
    reviews, outcomes,
    maintenance() { assert.ok(memory?.maintenance); return memory.maintenance; },
    consolidation() { assert.ok(memory?.consolidation); return memory.consolidation; },
    setReview(value: () => void | Promise<void>) { onReview = value; },
    async propose(proposal: import('../src/maintenance.ts').MaintenanceProposal, signal?: AbortSignal) {
      assert.ok(memory?.maintenance && context); return memory.maintenance.propose(context.sessionManager.getSessionId(), proposal, signal);
    },
    async restart() { await session.reload(); },
    async fork(beforeStart?: (parentFile: string) => Promise<void>) {
      const file = session.sessionManager.getSessionFile(); assert.ok(file);
      const manager = SessionManager.forkFrom(file, root, join(root, 'pi-sessions'));
      await beforeStart?.(file);
      await memory?.close(); session.dispose(); await resources.reload();
      ({ session } = await openSession(manager));
      await session.bindExtensions({ onError() {} });
      assert.ok(memory && context); await memory.rescan(context);
    },
    async work() { assert.ok(memory); return memory.work(session.sessionManager.getSessionId()); },
    status() { assert.ok(memory && context); return memory.status(context.sessionManager.getSessionId()); },
    async settings() { assert.ok(memory); return memory.settings(); },
    setInspect(value: (signal?: AbortSignal) => void | Promise<void>) { inspect = value; },
    setRawObserver(value: () => void) { rawObserver = value; },
    async rescan() { assert.ok(memory && context); await memory.rescan(context); },
    async streams() { assert.ok(memory); return memory.streams(session.sessionManager.getSessionId()); },
    async close() { await memory?.close(); session.dispose(); },
  };
}

test('native pi compaction persists sourced work across repeated extension reloads and preserves the current instruction', async t => {
  const summary = '## Goal\n合成：完成账本导出\n## Progress\n已完成列定义，尚未验证文件。\n## Next Steps\n核对导出列名。\n## Critical Context\n合成路径 ledger.csv；摘要不是写盘证明。';
  const host = await boot(summary, [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成原始任务：导出账本，金额保持两位小数。');
  await host.session.prompt('合成：先核对字段。'.repeat(100));
  await host.session.compact();
  const work = await host.work();
  assert.ok(work);
  assert.equal(work.state, 'unknown');
  assert.equal(work.goal_role, 'initial_user_request');
  assert.match(work.goal.text ?? '', /金额保持两位小数/);
  assert.match(JSON.stringify(work.progress), /Critical Context/);
  for (let index = 0; index < 2; index++) {
    await host.restart();
    await host.rescan();
    assert.equal((await host.work())?.checkpoint_id, work.checkpoint_id);
  }
  await host.session.prompt('合成当前指令：暂停导出，先回答列名。');
  const request = host.requests.at(-1)!;
  assert.match(JSON.stringify(request), /合成当前指令：暂停导出，先回答列名/);
  assert.match(JSON.stringify(request), /untrusted_work_continuation/);
  assert.doesNotMatch(JSON.stringify(request), /The conversation history before this point was compacted/);
});

test('native pi rejects a continuation invalidated during final request counting and excludes it on the next request', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：导出账本。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  host.faux.setResponses([context => {
    host.requests.push(structuredClone(context));
    return fauxAssistantMessage('## Progress\n合成旧进展-PI79\n## Next Steps\n核对列名。');
  }]);
  await host.session.compact();
  host.faux.setResponses(Array.from({ length: 4 }, () => context => {
    host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成普通回复。');
  }));
  const work = await host.work();
  assert.ok(work?.progress[0]);
  const count = host.memoryConfig.memoryBudget!.countRequest;
  let changed = false;
  host.memoryConfig.memoryBudget!.countRequest = request => {
    if (!changed) {
      changed = true;
      execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-race','main','confirm'),'user',1,None,'complete','合成用户确认：撤回旧摘要。')); r.change(a,Change('suppress-pi-summary',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
        host.paths.config, work.progress[0].message_id], { cwd: checkout });
    }
    return count(request);
  };
  const before = host.requests.length;
  await host.session.prompt('合成：现在仅回答列名。');
  assert.equal(host.requests.length, before);
  assert.equal(host.status().error_code, 'continuation_changed');
  host.memoryConfig.memoryBudget!.countRequest = count;
  await host.session.prompt('合成：重新仅回答列名。');
  assert.equal(host.requests.length, before + 1);
  assert.doesNotMatch(JSON.stringify(host.requests.at(-1)), /合成旧进展-PI79/);
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi summary remains dependent on its separately saved provider output', async t => {
  const host = await boot('合成：摘要输出。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：整理字段。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  await host.session.compact();
  const entry = host.session.sessionManager.getBranch().findLast(item => item.type === 'compaction');
  assert.ok(entry?.type === 'compaction');
  const details = entry.details as { evernook_work_v1: { outputs: import('../src/client.ts').Source[] } };
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const raw = await client.call<{ receipt: { message_id: string } }>('read_source', { source: details.evernook_work_v1.outputs[0] });
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-output','main','confirm'),'user',1,None,'complete','合成用户确认：撤回摘要输出。')); r.change(a,Change('suppress-pi-output',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
    host.paths.config, raw.receipt.message_id], { cwd: checkout });
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi file fork retains parent source controls and another paired entry reads the same work', async t => {
  const host = await boot('合成：字段检查。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成父来源：金额必须两位小数。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  await host.session.compact();
  const parent = await host.work(); assert.ok(parent);
  const previousSession = host.session.sessionManager.getSessionId();
  const parentFile = host.session.sessionManager.getSessionFile(); assert.ok(parentFile);
  await host.fork();
  assert.notEqual(host.session.sessionManager.getSessionId(), previousSession);
  assert.equal((await host.work())?.checkpoint_id, parent.checkpoint_id);
  assert.equal((await host.work())?.goal.message_id, parent.goal.message_id);
  await host.session.prompt('合成：分叉后的新问题。');
  await writeFile(parentFile, (await readFile(parentFile, 'utf8')).split('\n')[0] + '\n');
  await host.restart(); await host.rescan();
  assert.equal((await host.work())?.goal.message_id, parent.goal.message_id);
  const access = join(host.root, 'memory/mcp-access-dsh.json');
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-dsh',entry='dsh',access_path=Path(sys.argv[2]),scopes=('capture','recall'))", host.paths.config, access], { cwd: checkout });
  const other = new MemoryClient({ ...host.memoryConfig, entry: 'dsh', access });
  await other.connect(); t.after(() => other.close());
  const cross = await other.call<import('../src/client.ts').WorkView>('continuation_current', { task_id: parent.task_id, branch_id: parent.branch_id });
  assert.equal(cross.checkpoint_id, parent.checkpoint_id);
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-fork','main','confirm'),'user',1,None,'complete','合成用户确认：撤回父来源。')); r.change(a,Change('suppress-pi-parent',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
    host.paths.config, parent.goal.message_id!], { cwd: checkout });
  await host.rescan();
  assert.equal((await host.work())?.state, 'source_unavailable');
  await host.session.prompt('合成当前：仅列出列名。');
  assert.doesNotMatch(JSON.stringify(host.requests.at(-1)), /合成父来源：金额必须两位小数/);
  await host.fork();
  assert.equal((await host.work())?.state, 'source_unavailable');
  const afterNested = await other.recall('合成父来源：金额必须两位小数');
  assert.equal(afterNested.hits.filter(hit => hit.evidence.role === 'user').length, 0);
});

test('native pi tree navigation keeps abandoned and active work on their own checkpoint paths', async t => {
  const host = await boot('合成：字段检查。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：旧任务整理字段。');
  const forkPoint = host.session.sessionManager.getLeafId(); assert.ok(forkPoint);
  await host.session.prompt('合成：旧分支继续检查。'.repeat(100));
  await host.session.compact();
  const old = await host.work(); assert.ok(old && old.goal.text);
  const oldCompact = host.session.sessionManager.getBranch().findLast(item => item.type === 'compaction'); assert.ok(oldCompact);
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const span = (item: import('../src/client.ts').WorkView['goal']) => ({ message_id: item.message_id,
    state_version: item.state_version, content_version: item.content_version, representation: item.representation,
    start: item.start, end: item.end, expected_text: item.text });
  await client.call('continuation_save', { request_id: 'synthetic-abandon-old-pi-work', draft: {
    task_id: old.task_id, branch_id: old.branch_id, expected_revision: old.revision,
    anchor_id: old.progress[0].message_id, goal: span(old.goal), progress: old.progress.map(span), pending: [],
    state: 'abandoned', goal_role: old.goal_role, expires_at: old.expires_at, consumed_checkpoints: [old.checkpoint_id] } });
  assert.equal((await host.work())?.state, 'abandoned');
  await host.session.prompt('合成：废弃分支不再继续摘要。'.repeat(40));
  const beforeAbandoned = host.requests.length;
  await assert.rejects(host.session.compact(), /cancel/i);
  assert.equal(host.requests.length, beforeAbandoned);
  await host.session.navigateTree(forkPoint, { summarize: false });
  assert.equal(await host.work(), null);
  await host.session.prompt('合成当前新任务：列名按字母排序。'.repeat(40));
  await host.session.compact();
  const active = await host.work(); assert.ok(active);
  assert.notEqual(active.branch_id, old.branch_id);
  assert.equal(active.state, 'unknown');
  const activeCompact = host.session.sessionManager.getBranch().findLast(item => item.type === 'compaction'); assert.ok(activeCompact);
  await host.session.navigateTree(oldCompact.id, { summarize: false });
  assert.equal((await host.work())?.state, 'abandoned');
  assert.deepEqual((await host.work())?.progress, []);
  await host.session.navigateTree(activeCompact.id, { summarize: false });
  assert.equal((await host.work())?.checkpoint_id, active.checkpoint_id);
});

test('native pi excludes a suppressed source still retained beside the compaction checkpoint', async t => {
  const host = await boot('合成：普通字段回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：导出表格。');
  await host.session.prompt('合成保留原文-KEEP92：这个来源随后撤回。'.repeat(100));
  await host.session.compact();
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const recalled = await client.recall('合成保留原文-KEEP92');
  const hit = recalled.hits.find(item => item.evidence.role === 'user'); assert.ok(hit);
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-kept','main','confirm'),'user',1,None,'complete','合成用户确认：撤回保留原文。')); r.change(a,Change('suppress-pi-kept',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
    host.paths.config, hit.evidence.message_id], { cwd: checkout });
  const before = host.requests.length;
  await host.session.prompt('合成当前：现在只回答列名。');
  assert.equal(host.requests.length, before + 1);
  assert.doesNotMatch(JSON.stringify(host.requests.at(-1)), /合成保留原文-KEEP92/);
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi budgeted tool round trip remains usable and compaction depends on the saved tool output', async t => {
  const tool: ToolDefinition = { name: 'synthetic_columns', label: 'Synthetic columns', description: 'Return synthetic columns',
    parameters: Type.Object({ amount: Type.Number() }), async execute() {
      return { content: [{ type: 'text', text: '合成工具原文-TOOL93：amount,date' }], details: {} };
    } };
  const host = await boot('合成：已核对字段。', [tool], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：查询列名。');
  assert.equal(host.requests.length, 2);
  assert.equal(host.status().error_code, undefined);
  await host.session.prompt('合成：继续核对字段。'.repeat(100));
  await host.session.compact();
  assert.ok(await host.work());
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const recalled = await client.recall('合成工具原文-TOOL93', 32);
  const hit = recalled.hits.find(item => item.evidence.role === 'tool' && item.evidence.source.event.startsWith('tool-result:'));
  assert.ok(hit);
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-tool','main','confirm'),'user',1,None,'complete','合成用户确认：撤回工具输出。')); r.change(a,Change('suppress-pi-tool',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
    host.paths.config, hit.evidence.message_id], { cwd: checkout });
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi counts complete long-summary payloads and reports tiny-budget omissions with ordinary deep reads', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：整理表格。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  const counts: number[] = [];
  const original = host.memoryConfig.memoryBudget!.countRequest;
  host.memoryConfig.memoryBudget!.countRequest = request => { const used = original(request); counts.push(used); return used; };
  const summary = '## Goal\n合成账本\n## Progress\n' + 'hello world '.repeat(1000) + '\n## Next Steps\n核对列名。\n## Critical Context\n原文保留。';
  host.faux.setResponses([context => { host.requests.push(structuredClone(context)); return fauxAssistantMessage(summary); }]);
  await host.session.compact();
  const work = await host.work(); assert.ok(work?.progress[0]);
  const saved = JSON.parse(await readFile(host.memoryConfig.contextConfig!, 'utf8'));
  saved.policy.normal_tokens = 128;
  await writeFile(host.memoryConfig.contextConfig!, JSON.stringify(saved));
  host.memoryConfig.memoryBudget!.memoryTokens = 256;
  host.faux.setResponses([context => { host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成普通回复。'); }]);
  await host.session.prompt('合成当前：只回答列名。');
  const payload = host.requests.at(-1)!.messages.filter(message => message.role === 'user')
    .flatMap(message => typeof message.content === 'string' ? [message.content] : message.content.filter(item => item.type === 'text').map(item => item.text))
    .find(text => text.includes('untrusted_memory_context'));
  assert.ok(payload);
  const tokens = host.fixture!.target.encode(payload, { add_special_tokens: false }).ids.length;
  assert.ok(tokens <= 256);
  assert.equal(host.status().context?.provider_payload_tokens, tokens);
  assert.ok(host.status().context?.degraded.includes('context_envelope_over_budget'));
  assert.match(payload, /omitted|deep_fetch/);
  assert.ok(counts.length >= 2);
  assert.ok(counts.every(used => used + 1000 <= 20000));
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const originalSource = await client.read(work.progress[0].message_id);
  assert.equal(originalSource.event.text, summary);
  assert.equal((await host.settings()).host?.memory_tokens, 256);
});

test('native pi links a text-bearing tool-call message before the tool captures the current history', async t => {
  const tool: ToolDefinition = { name: 'synthetic_columns', label: 'Synthetic columns', description: 'Return synthetic columns',
    parameters: Type.Object({ amount: Type.Number() }), async execute() {
      return { content: [{ type: 'text', text: '合成：amount,date' }], details: {} };
    } };
  const host = await boot('合成普通回复。', [tool], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  host.faux.setResponses([context => {
    host.requests.push(structuredClone(context));
    const first = fauxAssistantMessage(fauxToolCall(tool.name, { amount: 37.5 }), { stopReason: 'toolUse' });
    first.content.unshift({ type: 'text', text: '合成：现在查询列名。' });
    return first;
  }, context => { host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成：查询结束。'); }]);
  await host.session.prompt('合成：查询列名。');
  assert.equal(host.requests.length, 2);
  assert.equal(host.status().error_code, undefined);
  assert.match(JSON.stringify(host.requests.at(-1)), /合成：现在查询列名/);
});

test('native pi auxiliary compaction screens protected originals without a default-provider fallback', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('password=synthetic-pi-compaction-123').catch(() => {});
  assert.equal(host.requests.length, 0);
  await host.session.prompt('合成：后续安全问题。'.repeat(100));
  const before = host.requests.length;
  assert.equal(before, 1);
  await assert.rejects(host.session.compact(), /cancel/i);
  assert.equal(host.requests.length, before);
  assert.doesNotMatch(JSON.stringify(host.requests), /synthetic-pi-compaction-123/);
  assert.equal(await host.work(), null);
});

test('native pi auxiliary compaction enforces the complete request and output reserve before target dispatch', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：整理表格。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  const original = host.memoryConfig.memoryBudget!.countRequest;
  let counted = false;
  host.memoryConfig.memoryBudget!.countRequest = request => {
    assert.match(JSON.stringify(request.context), /<conversation>/);
    const used = original(request);
    host.memoryConfig.memoryBudget!.contextWindow = used + host.memoryConfig.memoryBudget!.outputReserveTokens - 1;
    counted = true;
    return used;
  };
  const before = host.requests.length;
  await assert.rejects(host.session.compact(), /cancel/i);
  assert.equal(counted, true);
  assert.equal(host.requests.length, before);
  assert.equal(host.status().error_code, 'host_context_window_exceeded');
});

test('native pi repeated compaction bounds visible inputs rather than unrelated accumulated session metadata', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：整理表格。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  await host.session.compact();
  const first = await host.work(); assert.ok(first);
  for (let index = 0; index < 260; index++) host.session.sessionManager.appendSessionInfo('synthetic-metadata-' + index);
  await host.session.prompt('合成：继续核对列名。'.repeat(100));
  await host.session.compact();
  const next = await host.work(); assert.ok(next);
  assert.notEqual(next.checkpoint_id, first.checkpoint_id);
  assert.equal(next.state, 'unknown');
});

test('native pi refuses a fork whose initial inherited source cannot be proved from the ancestor', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成原始来源-ANCESTOR94。');
  const before = host.requests.length;
  await assert.rejects(host.fork(async parent => {
    const original = await readFile(parent, 'utf8');
    await writeFile(parent, original.split('\n')[0] + '\n');
  }), /continuation_origin_unavailable/);
  await host.session.prompt('合成当前：保持受限。').catch(() => {});
  assert.equal(host.requests.length, before);
});

test('native pi later compaction excludes the abandoned-branch summary already excluded from ordinary context', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：共享初始任务。'.repeat(100));
  const point = host.session.sessionManager.getLeafId(); assert.ok(point);
  await host.session.prompt('合成：废弃路径进展。'.repeat(100));
  host.faux.setResponses([context => {
    host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成旧分支摘要-BRANCH95');
  }]);
  const navigated = await host.session.navigateTree(point, { summarize: true });
  assert.equal(navigated.cancelled, false);
  assert.ok(navigated.summaryEntry);
  host.faux.setResponses(Array.from({ length: 4 }, () => context => {
    host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成当前路径摘要。');
  }));
  await host.session.prompt('合成当前任务：只核对列名。'.repeat(100));
  assert.doesNotMatch(JSON.stringify(host.requests.at(-1)), /合成旧分支摘要-BRANCH95/);
  const before = host.requests.length;
  await host.session.compact();
  assert.ok(host.requests.length > before);
  assert.doesNotMatch(JSON.stringify(host.requests.slice(before)), /合成旧分支摘要-BRANCH95/);
  assert.equal((await host.work())?.state, 'unknown');
});

test('native pi auxiliary dispatch validates the exact previous work consumed before its inner capture', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  host.memoryConfig.memoryBudget!.memoryTokens = 4096;
  await host.session.prompt('合成：整理表格。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  host.faux.setResponses([context => {
    host.requests.push(structuredClone(context)); return fauxAssistantMessage('## Progress\n合成旧辅助摘要-AUX96\n## Next Steps\n继续核对。');
  }]);
  await host.session.compact();
  const previous = await host.work(); assert.ok(previous?.progress[0]);
  for (let index = 0; index < 24; index++) host.session.sessionManager.appendMessage({ role: 'user',
    content: '合成：待压缩的独立原生输入 ' + index, timestamp: Date.now() });
  const source = { entry: 'pi', session: host.session.sessionManager.getSessionId(), branch: 'work', event: 'work-input:'
    + createHash('sha256').update(JSON.stringify([host.session.sessionManager.getLeafId(), previous.checkpoint_id])).digest('hex') };
  const script = `import json,sys,time
from pathlib import Path
from evernook import pair_client,MemoryRuntime,Event,Source,Change
c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c)
source=Source(**json.loads(sys.argv[2])); print('ready',flush=True); deadline=time.monotonic()+20
while r.read_source(a,source) is None:
 if time.monotonic()>deadline: raise RuntimeError('synthetic proof timeout')
 time.sleep(0.001)
confirmation=r.submit(a,Event(Source('owner','synthetic-pi-aux-race','main','confirm'),'user',1,None,'complete','合成用户确认：撤回辅助摘要。'))
r.change(a,Change('suppress-pi-aux-race',sys.argv[3],1,'suppress',confirmation.message_id,'synthetic confirmation'))
print('suppressed',flush=True)
`;
  const worker = spawn(python!, ['-u', '-c', script, host.paths.config, JSON.stringify(source), previous.progress[0].message_id],
    { cwd: checkout, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => { if (worker.exitCode === null) worker.kill(); });
  let output = '';
  let ready!: () => void;
  const initialized = new Promise<void>(resolve => { ready = resolve; });
  worker.stdout.on('data', chunk => { output += String(chunk); if (output.includes('ready')) ready(); });
  const exited = new Promise<void>((resolve, reject) => {
    worker.once('error', reject);
    worker.once('exit', code => code === 0 ? resolve() : reject(new Error('synthetic owner worker failed')));
  });
  await Promise.race([initialized, exited.then(() => { throw new Error('synthetic owner exited before ready'); })]);
  host.faux.setResponses([context => { host.requests.push(structuredClone(context)); return fauxAssistantMessage('合成新摘要。'); }]);
  const before = host.requests.length;
  const result = await host.session.compact().then(() => null, error => error);
  await exited;
  assert.match(output, /suppressed/);
  assert.equal(host.requests.length, before);
  assert.match(String(result), /cancel/i);
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi durable compaction binds committed stream originals and restores after reload', async t => {
  const host = await boot('合成可靠摘要🙂。', [], undefined, true, false, false, true, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成：整理账本。');
  await host.session.prompt('合成：核对字段。'.repeat(100));
  await host.session.compact();
  const work = await host.work(); assert.ok(work);
  assert.equal(work.state, 'unknown');
  assert.match(JSON.stringify(work.progress), /合成可靠摘要/);
  await host.restart(); await host.rescan();
  assert.equal((await host.work())?.checkpoint_id, work.checkpoint_id);
  const stream = (await host.streams()).streams[0]; assert.ok(stream);
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const page = await client.readStream(stream); assert.ok(page.final_message_id);
  execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-durable','main','confirm'),'user',1,None,'complete','合成用户确认：撤回可靠原文。')); r.change(a,Change('suppress-pi-durable',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
    host.paths.config, page.final_message_id], { cwd: checkout });
  assert.equal((await host.work())?.state, 'source_unavailable');
});

test('native pi first compaction revalidates actual input sources changed by the final counter', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成第一次压缩原文-AUX97。');
  await host.session.prompt('合成：检查字段。'.repeat(100));
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const hit = (await client.recall('合成第一次压缩原文-AUX97')).hits.find(item => item.evidence.role === 'user'); assert.ok(hit);
  const original = host.memoryConfig.memoryBudget!.countRequest;
  let changed = false;
  host.memoryConfig.memoryBudget!.countRequest = request => {
    if (!changed) {
      changed = true;
      execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-first-aux','main','confirm'),'user',1,None,'complete','合成用户确认：撤回压缩原文。')); r.change(a,Change('suppress-pi-first-aux',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
        host.paths.config, hit.evidence.message_id], { cwd: checkout });
    }
    return original(request);
  };
  const before = host.requests.length;
  const result = await host.session.compact().then(() => null, error => error);
  assert.equal(changed, true);
  assert.equal(host.requests.length, before);
  assert.match(String(result), /cancel/i);
  assert.equal(await host.work(), null);
});

test('native pi ordinary dispatch also rechecks a retained original changed by final counting', async t => {
  const host = await boot('合成普通回复。', [], undefined, true, false, false, false, { compaction: true });
  t.after(() => host.close());
  await host.session.prompt('合成普通历史原文-RAW98。');
  const client = new MemoryClient(host.memoryConfig); await client.connect(); t.after(() => client.close());
  const hit = (await client.recall('合成普通历史原文-RAW98')).hits.find(item => item.evidence.role === 'user'); assert.ok(hit);
  const original = host.memoryConfig.memoryBudget!.countRequest;
  let changed = false;
  host.memoryConfig.memoryBudget!.countRequest = request => {
    if (!changed) {
      changed = true;
      execFileSync(python!, ['-c', "import sys; from pathlib import Path; from evernook import pair_client,MemoryRuntime,Event,Source,Change; c=Path(sys.argv[1]); a=pair_client(c,client_id='synthetic-owner',entry='owner',access_path=c.parent/'mcp-access-owner.json'); r=MemoryRuntime(c); confirmation=r.submit(a,Event(Source('owner','synthetic-pi-raw-race','main','confirm'),'user',1,None,'complete','合成用户确认：撤回历史原文。')); r.change(a,Change('suppress-pi-raw-race',sys.argv[2],1,'suppress',confirmation.message_id,'synthetic confirmation'))",
        host.paths.config, hit.evidence.message_id], { cwd: checkout });
    }
    return original(request);
  };
  const before = host.requests.length;
  await host.session.prompt('合成当前：仅核对列名。');
  assert.equal(host.requests.length, before);
  host.memoryConfig.memoryBudget!.countRequest = original;
  await host.session.prompt('合成当前：重新核对列名。');
  assert.equal(host.requests.length, before + 1);
  assert.doesNotMatch(JSON.stringify(host.requests.at(-1)), /合成普通历史原文-RAW98/);
});

test('native pi lifecycle gates background work and reload catches up without changing actor scopes', async t => {
  const host = await boot('合成：前台 pi 输出', [], undefined, false, false, false, false, { consolidation: true });
  t.after(() => host.close());
  const background = host.consolidation();
  let checked = false;
  host.setInspect(async () => {
    assert.equal((await background.status()).foreground_sessions, 1);
    execFileSync(python!, ['-m', 'evernook.cli', 'scheduler-configure', '--config', host.paths.config,
      '--access', join(host.root, 'memory/mcp-access-owner.json')], { cwd: checkout, input: JSON.stringify({ paused: false }) });
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal((await background.status()).processed, 0);
    checked = true;
  });
  await host.session.prompt('合成：前台结束后处理');
  assert.equal(checked, true);
  const deadline = Date.now() + 20000;
  let status = await background.status();
  while ((status.pending !== 0 || status.foreground_sessions !== 0) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100)); status = await background.status();
  }
  assert.equal(status.pending, 0);
  assert.equal(status.foreground_sessions, 0);
  assert.ok(Number(status.covered_messages) > 0);
  assert.doesNotMatch(JSON.stringify(host.requests), /mcp-access-(consolidation|owner)/);
  await host.restart();
  const resumed = await host.consolidation().status();
  assert.equal(resumed.foreground_sessions, 0);
  assert.ok(Number(resumed.session_marks) > 0);
});

test('native pi provider uses admitted preferences and trusted suppression excludes profile and fact after reload', async t => {
  const host = await boot('合成偏好回复。', [], undefined, true, false, true, false, { consolidation: true });
  t.after(() => host.close());
  const owner = join(host.root, 'memory/mcp-access-owner.json');
  function cli(command: string, input: Record<string, unknown> = {}) {
    return JSON.parse(execFileSync(python!, ['-m', 'evernook.cli', command, '--config', host.paths.config, '--access', owner],
      { cwd: checkout, input: JSON.stringify(input), encoding: 'utf8' })).result;
  }
  const saved = JSON.parse(await readFile(host.memoryConfig.contextConfig!, 'utf8'));
  Object.assign(saved.policy, { normal_tokens: 4096, deep_tokens: 8192, profile_tokens: 1000,
    fact_keys: [['user', 'preference:answer_verbosity']] });
  await writeFile(host.memoryConfig.contextConfig!, JSON.stringify(saved));
  host.memoryConfig.memoryBudget!.memoryTokens = 4096;
  cli('preference-update-configure', { enabled: true,
    admitted_processor: cli('preference-update-status').settings.rules_processor });
  cli('scheduler-configure', { paused: false, max_trigger_seconds: 10 });
  await host.session.prompt('我偏好简短回答。');
  const deadline = Date.now() + 30000;
  let tags = cli('profile-summary').tags;
  while (!tags.length && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 200)); tags = cli('profile-summary').tags;
  }
  assert.equal(tags.length, 1);
  function payload() {
    const memory = host.requests.at(-1)!.messages.filter(message => message.role === 'user')
      .flatMap(message => typeof message.content === 'string' ? [message.content]
        : message.content.filter(block => block.type === 'text').map(block => block.text))
      .filter(text => text.includes('untrusted_memory_context'));
    assert.equal(memory.length, 1);
    return JSON.parse(memory[0]) as { items: { kind: string; data: { label?: string; fact?: { text: string } | null } }[];
      coverage: { lexical: { state: string } } };
  }
  await host.session.prompt('合成：本轮请用表格，当前指令优先。');
  assert.ok(payload().items.some(item => item.kind === 'profile' && item.data.label === '我偏好简短回答。'));
  assert.ok(payload().items.some(item => item.kind === 'facts' && item.data.fact?.text === '我偏好简短回答。'));
  assert.equal(payload().coverage.lexical.state, 'not_built');
  assert.match(JSON.stringify(host.requests.at(-1)), /本轮请用表格/);
  const review = await host.propose({ kind: 'profile', tag_id: tags[0].tag_id, action: 'suppress' });
  await host.session.prompt(review.confirm_command);
  assert.equal(host.outcomes.at(-1)?.state, 'completed');
  await host.restart();
  await host.session.prompt('合成：重载后继续当前聊天。');
  assert.equal(payload().items.some(item => item.kind === 'profile' && item.data.label === '我偏好简短回答。'), false);
  assert.equal(payload().items.some(item => item.kind === 'facts' && item.data.fact?.text === '我偏好简短回答。'), false);
  assert.doesNotMatch(JSON.stringify(host.requests), /mcp-access-(owner|consolidation|maintenance)/);
});

test('pi durable text publishes only committed prefixes before the full answer is saved and survives reload', async () => {
  const reply = '合成可靠分块🙂之后仍继续逐块生成。';
  const host = await boot(reply, [], undefined, true, false, false, true);
  const reader = new MemoryClient(host.memoryConfig);
  await reader.connect();
  const displayed: string[] = [];
  const checks: Promise<void>[] = [];
  host.session.subscribe(event => {
    if (event.type !== 'message_update' || event.assistantMessageEvent.type !== 'text_delta') return;
    displayed.push(event.assistantMessageEvent.delta);
    if (checks.length) return;
    const first = displayed.join('');
    checks.push((async () => {
      const found = await host.streams();
      const page = await reader.readStream(found.streams[0]);
      assert.ok(page.text.startsWith(first));
      assert.equal(page.state, 'open');
      assert.equal(page.final_message_id, null);
    })());
    void checks.at(-1)!.catch(() => {});
  });
  try {
    await host.session.prompt('合成：pi 可靠输出');
    await Promise.all(checks);
    assert.ok(checks.length);
    assert.equal(displayed.join(''), reply);
    assert.deepEqual(host.rawChecks, []);
    await host.restart();
    await host.session.prompt('/evernook-rescan'); await host.session.prompt('/evernook-rescan');
    const found = await host.streams();
    assert.equal(found.streams.length, 1);
    const page = await reader.readStream(found.streams[0]);
    assert.equal(page.text, reply);
    assert.equal(page.state, 'complete');
  } finally { await reader.close(); await host.close(); }
});

test('pi durable cancellation recovers only committed partial text after restart and duplicate rescan', async () => {
  const host = await boot('已显示之后还有许多合成文字🙂pass', [], undefined, false, false, false, true);
  const visible: string[] = [];
  let abort: Promise<void> | undefined;
  host.session.subscribe(event => {
    if (event.type !== 'message_update' || event.assistantMessageEvent.type !== 'text_delta') return;
    visible.push(event.assistantMessageEvent.delta);
    abort ??= host.session.abort();
  });
  try {
    await host.session.prompt('合成：取消 pi 流');
    await abort;
    assert.ok(visible.join(''));
    await host.restart();
    await host.session.prompt('/evernook-rescan'); await host.session.prompt('/evernook-rescan');
    const found = await host.streams();
    const reader = new MemoryClient(host.memoryConfig);
    await reader.connect();
    try {
      const page = await reader.readStream(found.streams[0]);
      assert.ok(page.text.startsWith(visible.join('')));
      assert.equal(page.state, 'canceled');
      assert.equal(page.complete, false);
      assert.equal(page.final_message_id, null);
      assert.deepEqual(await reader.readStream(found.streams[0]), page);
    } finally { await reader.close(); }
  } finally { await host.close(); }
});

test('pi durable mid-stream storage failure prevents unsaved publication and preserves the saved prefix', async () => {
  const reply = '已显示之后还有许多合成文字用于失败验证。';
  const host = await boot(reply, [], undefined, false, false, false, true);
  const visible: string[] = [];
  host.session.subscribe(event => {
    if (event.type !== 'message_update' || event.assistantMessageEvent.type !== 'text_delta') return;
    visible.push(event.assistantMessageEvent.delta);
    if (visible.length !== 1) return;
    const db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'));
    try { db.exec("CREATE TRIGGER synthetic_pi_chunk_failure BEFORE INSERT ON text_stream_chunks_v1 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"); }
    finally { db.close(); }
  });
  try {
    await host.session.prompt('合成：pi 写入中断');
    assert.ok(visible.join(''));
    assert.notEqual(visible.join(''), reply);
    const found = await host.streams();
    const reader = new MemoryClient(host.memoryConfig); await reader.connect();
    try {
      const page = await reader.readStream(found.streams[0]);
      assert.equal(page.text, visible.join(''));
      assert.equal(page.state, 'open');
      assert.equal(page.final_message_id, null);
    } finally { await reader.close(); }
  } finally { await host.close(); }
});

test('pi durable sensitive and unsupported tool output never leaks through snapshots or raw observers', async () => {
  for (const tool of [false, true]) {
    const host = await boot('password=synthetic-pi-durable-secret', [], undefined, false, false, false, true);
    if (tool) host.faux.setResponses([fauxAssistantMessage(fauxToolCall('unwrapped-tool', { secret: 'synthetic-pi-durable-secret' }), { stopReason: 'toolUse' })]);
    const visible: string[] = [];
    host.session.subscribe(event => { if (event.type === 'message_update') visible.push(JSON.stringify(event)); });
    try {
      await host.session.prompt('合成：pi 保护测试');
      assert.doesNotMatch(visible.join(''), /password|synthetic-pi-durable-secret|unwrapped-tool/);
      assert.deepEqual(host.rawChecks, []);
      const found = await host.streams();
      const reader = new MemoryClient(host.memoryConfig); await reader.connect();
      try {
        const page = await reader.readStream(found.streams[0]);
        assert.equal(page.text, '');
        assert.equal(page.state, tool ? 'failed' : 'protected');
      } finally { await reader.close(); }
      const paths = await readdir(join(host.root, 'memory'));
      const contents = Buffer.concat(await Promise.all(paths.filter(name => name.startsWith('memory.sqlite3')).map(name => readFile(join(host.root, 'memory', name)))));
      assert.ok(!contents.includes(Buffer.from('synthetic-pi-durable-secret')));
    } finally { await host.close(); }
  }
});

test('pi durable first-fragment timing measures synthetic provider delta receipt to publication', async () => {
  const host = await boot('合成可靠分块首片测量🙂。', [], undefined, false, false, false, true);
  const samples: { first_delta_to_publication_ms: number; request_to_publication_ms: number }[] = [];
  try {
    for (let index = 0; index < 5; index++) {
      await host.session.prompt('合成：首片测量' + index);
      const timing = host.status().stream_timing;
      assert.ok(timing);
      assert.ok(timing.first_delta_to_publication_ms >= 0);
      assert.ok(timing.request_to_publication_ms >= timing.first_delta_to_publication_ms);
      samples.push(timing);
    }
    process.stdout.write('PI_DURABLE_SYNTHETIC_TIMING ' + JSON.stringify(samples) + '\n');
  } finally { await host.close(); }
});

test('pi durable length and provider errors preserve partial evidence without importing a complete answer', async () => {
  for (const stopReason of ['length', 'error'] as const) {
    const host = await boot('已显示pass', [], undefined, false, false, false, true);
    host.faux.setResponses([fauxAssistantMessage('已显示pass', { stopReason,
      errorMessage: 'password=synthetic-provider-diagnostic' })]);
    const visible: string[] = [];
    host.session.subscribe(event => { if (event.type === 'message_update') visible.push(JSON.stringify(event)); });
    try {
      await host.session.prompt('合成：输出终态');
      assert.doesNotMatch(visible.join(''), /synthetic-provider-diagnostic|password/);
      await host.session.prompt('/evernook-rescan');
      const found = await host.streams();
      const reader = new MemoryClient(host.memoryConfig); await reader.connect();
      try {
        const page = await reader.readStream(found.streams[0]);
        assert.equal(page.state, 'failed');
        assert.equal(page.text, '已显示');
        assert.equal(page.pending_chars, 4);
        assert.equal(page.final_message_id, null);
        const matches = await reader.call<{ hits: { role: string; completion: string }[] }>('search', { query: '已显示', mode: 'exact' });
        assert.ok(!matches.hits.some(hit => hit.role === 'assistant' && hit.completion === 'complete'));
      } finally { await reader.close(); }
    } finally { await host.close(); }
  }
});

test('pi confirmation shares the exact-word and independent maintenance contract and rejection never writes', async () => {
  const host = await boot(undefined, [], undefined, false, false, true);
  try {
    const words = '合成 pi 重要原话37.50元🙂\r\ne\u0301'.repeat(160);
    await host.session.prompt(words);
    const id = host.rows().find(row => row.event.text === words)!.receipt.message_id;
    const rejected = await host.propose({ kind: 'pin', message_id: id });
    await host.maintenance().reject(rejected.proposal_id);
    await host.session.prompt(rejected.confirm_command);
    assert.equal(host.maintenance().status(rejected.proposal_id)?.state, 'rejected');
    const review = await host.propose({ kind: 'pin', message_id: id });
    assert.equal(host.maintenance().status(review.proposal_id)?.state, 'pending');
    await host.session.prompt(review.confirm_command);
    const result = host.maintenance().status(review.proposal_id);
    assert.equal(result?.state, 'completed');
    const important = result?.result?.important_id; assert.equal(typeof important, 'string');
    let restored = ''; let cursor: number | null = 0;
    while (cursor !== null) {
      const page = await host.maintenance().readImportant(important as string, cursor, 512);
      restored += page.text; cursor = page.next_cursor;
    }
    assert.equal(restored, words);
    assert.equal(host.outcomes.filter(outcome => outcome.state === 'completed').length, 1);
  } finally { await host.close(); }
});

test('pi actual model tool only proposes and canceling a trusted review never grants an executable operation', async () => {
  const host = await boot(undefined, [], undefined, false, false, true);
  try {
    await host.session.prompt('合成 pi 工具重要原话');
    const id = host.rows().find(row => row.event.text === '合成 pi 工具重要原话')!.receipt.message_id;
    const controller = new AbortController();
    host.setReview(() => { controller.abort(); });
    await assert.rejects(host.propose({ kind: 'pin', message_id: id }, controller.signal));
    const canceled = host.reviews.at(-1)!;
    assert.equal(host.maintenance().status(canceled.proposal_id), undefined);
    await host.session.prompt(canceled.confirm_command);
    host.setReview(() => {});
    host.faux.setResponses([fauxAssistantMessage(fauxToolCall('evernook_memory_propose',
      { proposal: JSON.stringify({ kind: 'pin', message_id: id }) }), { stopReason: 'toolUse' }), fauxAssistantMessage('合成：请在宿主审查预览')]);
    await host.session.prompt('合成：提议记住这条');
    const review = host.reviews.at(-1)!;
    assert.notEqual(review.proposal_id, canceled.proposal_id);
    assert.equal(host.maintenance().status(review.proposal_id)?.state, 'pending');
    assert.equal(host.outcomes.length, 0);
    await host.session.prompt(review.confirm_command);
    assert.equal(host.maintenance().status(review.proposal_id)?.state, 'completed');
    assert.equal(host.outcomes.length, 1);
  } finally { await host.close(); }
});

test('pi source correction suppression and exact purge survive service restart without reviving a pending approval', async () => {
  const host = await boot(undefined, [], undefined, false, false, true);
  try {
    await host.session.prompt('合成：地址原值A');
    const target = host.rows().find(row => row.event.text === '合成：地址原值A')!.receipt.message_id;
    await host.session.prompt('合成：更正地址B');
    const replacement = host.rows().find(row => row.event.text === '合成：更正地址B')!.receipt.message_id;
    const corrected = await host.propose({ kind: 'correct_source', message_id: target, replacement_id: replacement });
    await host.session.prompt(corrected.confirm_command);
    assert.equal(host.maintenance().status(corrected.proposal_id)?.state, 'completed');
    assert.equal(host.maintenance().status(corrected.proposal_id)?.result?.status, 'corrected');
    const pending = await host.propose({ kind: 'suppress', message_id: replacement });
    const prior = host.maintenance();
    await host.restart();
    await assert.rejects(prior.propose('closed-native-runtime', { kind: 'pin', message_id: replacement }), /invalid_maintenance_proposal|aborted/i);
    await host.session.prompt(pending.confirm_command);
    assert.equal(host.maintenance().status(pending.proposal_id), undefined);
    assert.equal((await host.maintenance().readSource(replacement)).text, '合成：更正地址B');
    const suppressed = await host.propose({ kind: 'suppress', message_id: replacement });
    await host.session.prompt(suppressed.confirm_command);
    assert.equal(host.maintenance().status(suppressed.proposal_id)?.state, 'completed');
    await assert.rejects(host.maintenance().readSource(replacement), /message_not_found/);
    const purge = await host.propose({ kind: 'purge', source_ids: [target, replacement] });
    assert.deepEqual(purge.residuals, ['existing_archives_and_backups_retained', 'host_copies_not_controlled']);
    await host.session.prompt(purge.confirm_command);
    assert.equal(host.maintenance().status(purge.proposal_id)?.state, 'completed');
    const result = host.maintenance().status(purge.proposal_id)?.result;
    assert.deepEqual(result?.residuals, purge.residuals);
    await host.restart();
    await assert.rejects(host.maintenance().readSource(target), /message_not_found/);
    await assert.rejects(host.maintenance().readSource(replacement), /message_not_found/);
  } finally { await host.close(); }
});

test('pi uses the same real tokenizer core with live settings and independent full-request budget checks', async () => {
  const host = await boot(undefined, [], undefined, true);
  try {
    await host.session.prompt('hello world');
    assert.equal(host.requests.length, 1);
    const first = host.requests[0].messages.filter(message => message.role === 'user');
    assert.match(JSON.stringify(first), /untrusted_memory_context/);
    const memory = first.flatMap(message => typeof message.content === 'string' ? [message.content]
      : message.content.filter(block => block.type === 'text').map(block => block.text))
      .filter(text => text.includes('untrusted_memory_context'));
    assert.equal(memory.length, 1);
    assert.ok(host.fixture!.target.encode(memory[0], { add_special_tokens: false }).ids.length <= 512);
    assert.equal(host.status().context_mode, 'target_tokenizer');
    assert.equal((await host.settings()).context.saved_policy?.normal_tokens, 512);
    const clients = JSON.parse(await readFile(join(host.root, 'memory/client.json'), 'utf8')) as { clients: { client_id: string; scopes: string[] }[] };
    assert.deepEqual(clients.clients.find(client => client.client_id === 'synthetic-pi')?.scopes, ['capture', 'recall']);
    const saved = JSON.parse(await readFile(host.memoryConfig.contextConfig!, 'utf8'));
    saved.policy.normal_tokens = 256;
    saved.policy.recent_days = 1;
    await writeFile(host.memoryConfig.contextConfig!, JSON.stringify(saved));
    await host.session.prompt('x'.repeat(257));
    assert.equal(host.requests.length, 2);
    assert.equal(host.status().context?.token_limit, 256);
    assert.ok(host.status().context?.degraded.includes('query_too_long'));
    const current = host.requests[1].messages.filter(message => message.role === 'user');
    assert.equal(current.filter(message => JSON.stringify(message).includes('untrusted_memory_context')).length, 1);
    saved.policy.normal_tokens = true;
    await writeFile(host.memoryConfig.contextConfig!, JSON.stringify(saved));
    await host.session.prompt('hello');
    assert.equal(host.requests.length, 2);
    assert.equal(host.status().error_code, 'invalid_context_policy');
  } finally { await host.close(); }
});

test('pi refuses an exhausted full request window before delegated generation', async () => {
  const host = await boot(undefined, [], undefined, true);
  try {
    host.memoryConfig.memoryBudget!.countRequest = () => 1000000;
    await host.session.prompt('hello world');
    assert.equal(host.requests.length, 0);
    assert.equal(host.status().error_code, 'host_context_window_exceeded');
    assert.deepEqual(host.rawChecks, []);
  } finally { await host.close(); }
});

test('pi text-only budget rejects an image introduced after native capture by another context extension', async () => {
  const host = await boot(undefined, [], undefined, true, true);
  try {
    await host.session.prompt('hello world');
    assert.equal(host.requests.length, 0);
    assert.equal(host.status().error_code, 'unsupported_native_image');
  } finally { await host.close(); }
});

test('pi refuses a virtual target whose physical route could change after counting', async () => {
  const host = await boot(undefined, [], undefined, true);
  try {
    host.memoryConfig.target = { ...host.memoryConfig.target, api: 'pi-virtual' };
    await host.session.prompt('hello world');
    assert.equal(host.requests.length, 0);
    assert.equal(host.status().error_code, 'unsupported_budget_model');
  } finally { await host.close(); }
});

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
  const host = await boot(undefined, [], undefined, true, false, true);
  host.memoryConfig.memoryBudget!.memoryTokens = 2048;
  const piSaved = JSON.parse(await readFile(host.memoryConfig.contextConfig!, 'utf8'));
  piSaved.policy.normal_tokens = 2048;
  piSaved.policy.deep_tokens = 4096;
  await writeFile(host.memoryConfig.contextConfig!, JSON.stringify(piSaved));
  const script = "from pathlib import Path; import sys; from evernook import pair_client; pair_client(Path(sys.argv[1]),client_id='synthetic-cross-dsh',entry='dsh',access_path=Path(sys.argv[2]),scopes=('capture','recall'))";
  const dshAccess = join(host.root, 'memory/mcp-access-cross-dsh.json');
  execFileSync(python!, ['-c', script, host.paths.config, dshAccess], { cwd: checkout });
  const ctx = new DshContext();
  let memory: Awaited<ReturnType<typeof installMemory>> | undefined;
  let agent: Awaited<ReturnType<typeof ctx.agents.create>> | undefined;
  const dshRequests: GenerateOptions[] = [];
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
      async *stream(request: GenerateOptions): AsyncIterable<StreamChunk> {
        dshRequests.push(request);
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text: '合成dsh回复。' };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: '合成dsh回复。' } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    ctx.llm.registerAdapter(['synthetic-cross'], new SyntheticDsh());
    const fixture = await tokenizerFixture(host.root, 'offline');
    const dshSaved = JSON.parse(await readFile(fixture.contextConfig, 'utf8'));
    dshSaved.policy = piSaved.policy;
    await writeFile(fixture.contextConfig, JSON.stringify(dshSaved));
    memory = await installMemory(ctx, { python: python!, config: host.paths.config, access: dshAccess, entry: 'dsh', cwd: checkout,
      contextConfig: fixture.contextConfig, memoryBudget: { provider: 'synthetic-cross', modelId: 'offline', memoryTokens: 2048,
        contextWindow: 20000, outputReserveTokens: 1000,
        countRequest: request => fixture.target.encode(JSON.stringify({ messages: request.messages, tools: request.tools, system: request.system }), { add_special_tokens: false }).ids.length } });
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
    assert.match(JSON.stringify(injected), /untrusted_memory_context/);
    await host.session.prompt('合成：在pi记下周末爬山。');
    agent.agent.followup(freezeMessage({ id: MessageId('synthetic-back-user'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '爬山' }] }));
    await agent.agent.whenIdle();
    assert.match(JSON.stringify(dshRequests.at(-1)?.messages), /在pi记下周末爬山/);
    await memory.capture(agent.agent.session);
    await host.rescan();
    const count = host.rows().length;
    await memory.rescan(agent.agent.session.id);
    await host.rescan();
    assert.equal(host.rows().length, count);
    const piSource = host.rows().find(row => row.event.text === '合成：在pi记下周末爬山。')!.receipt.message_id;
    const suppression = await host.propose({ kind: 'suppress', message_id: piSource });
    await host.session.prompt(suppression.confirm_command);
    assert.equal(host.maintenance().status(suppression.proposal_id)?.state, 'completed');
    agent.agent.followup(freezeMessage({ id: MessageId('synthetic-after-pi-suppress'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '爬山' }] }));
    await agent.agent.whenIdle();
    assert.doesNotMatch(JSON.stringify(dshRequests.at(-1)?.messages), /在pi记下周末爬山/);
  } finally { await agent?.dispose(); await memory?.close(); await ctx.fiber.dispose(); await host.close(); }
});

test('pi cancellation during memory construction never dispatches to the target provider', async () => {
  const host = await boot(undefined, [], undefined, true);
  const pending = host.session.prompt('hello world');
  let interval: ReturnType<typeof setInterval> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const entered = new Promise<void>((resolve, reject) => {
      interval = setInterval(() => {
        let db: DatabaseSync | undefined;
        try {
          db = new DatabaseSync(join(host.root, 'memory/memory.sqlite3'), { readOnly: true });
          if (db.prepare("SELECT 1 FROM audit_events_v1 WHERE action='context_build' LIMIT 1").get()) resolve();
        } catch (error) {
          if (!(error instanceof Error && 'errcode' in error && typeof error.errcode === 'number'
              && [5, 6].includes(error.errcode & 255))) reject(error);
        } finally { db?.close(); }
      }, 10);
    });
    const deadline = new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new Error('context checkpoint not reached')), 15000); });
    await Promise.race([entered, deadline, pending.then(() => { throw new Error('request ended before context checkpoint'); })]);
    await host.session.abort();
    await pending.catch(() => {});
    assert.equal(host.requests.length, 0);
    assert.deepEqual(host.rawChecks, []);
  } finally {
    if (interval) clearInterval(interval);
    if (timer) clearTimeout(timer);
    await host.session.abort(); await pending.catch(() => {}); await host.close();
  }
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
