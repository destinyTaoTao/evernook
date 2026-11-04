import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { once } from 'node:events';
import { Socket } from 'node:net';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import type { GenerateOptions, StreamChunk } from '@deepseek-ai/dsh-llm';
import type { Api, AssistantMessage, Model, TranscriptContext } from '@earendil-works/pi-ai';

const [hostRoot, python, root, entry, trial, mode] = process.argv.slice(2);
if (mode && mode !== 'lock-first-commit') throw new Error('unknown_installation_mode');
const blocked = mode === 'lock-first-commit';
if (!['dsh', 'pi'].includes(entry)) throw new Error('installed_entry_required');
const loaderPath = join(hostRoot, 'installed-module-loader.mjs');
const loaderText = 'export const locate = name => import.meta.resolve(name);\n';
try { await writeFile(loaderPath, loaderText, { flag: 'wx' }); }
catch (failure) {
  if (!(failure instanceof Error) || !('code' in failure) || failure.code !== 'EEXIST'
      || await readFile(loaderPath, 'utf8') !== loaderText) throw failure;
}
const loader = await import(pathToFileURL(loaderPath).href) as { locate(name: string): string };
const imports: Record<string, string> = {};
async function load<T>(name: string): Promise<T> {
  const path = loader.locate(name); imports[name] = path;
  return await import(path) as T;
}
const fixture = JSON.parse(await readFile(join(root, 'fixture.json'), 'utf8'));
const output = join(root, 'native-' + entry + (trial ? '-' + trial : ''));
await mkdir(output);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const { MemoryClient } = await load<typeof import('../src/client.ts')>('@evernook/harnesses/client');
const config = { python, config: fixture.config, access: join(root, 'runtime/mcp-access-' + entry + '.json'), entry, cwd: root,
  streamMode: 'durable_text' as const };
const reader = new MemoryClient(config);
await reader.connect();
const expected = '合成SDK安装回执。';
let visible = '';
let observedEvidenceId = false;
let replayMatches = false;
let firstPrefixMatches = false;
let error: string | null = null;
let lock: ChildProcessWithoutNullStreams | undefined;
let lockReady = false;
let storageFailureObserved = false;
let storageFailureCode: string | null = null;
async function holdWriter() {
  lock = spawn(python, [resolve(import.meta.dirname, '../../../probes/installed/hold_write.py'), root],
    { cwd: root, windowsHide: true, stdio: 'pipe', env: { ...process.env, PYTHONPATH: '' } });
  const [data] = await once(lock.stdout, 'data', { signal: AbortSignal.timeout(10000) });
  if (String(data).trim() !== 'synthetic-writer-locked') throw new Error('writer_lock_not_established');
  lockReady = true;
}
const cleanups: (() => unknown | Promise<unknown>)[] = [];
const firstProofs: Promise<void>[] = [];
let sources: (() => Promise<import('../src/client.ts').Source[]>) | undefined;
const entered = (value: unknown) => { observedEvidenceId ||= JSON.stringify(value).includes(fixture.original_id); };
const publish = (text: string) => {
  visible += text;
  if (!text || firstProofs.length || !sources) return;
  const prefix = visible;
  firstProofs.push((async () => {
    const values = await sources!();
    firstPrefixMatches = values.length === 1 && (await reader.readStream(values[0])).text.startsWith(prefix);
  })());
};
try {
  if (entry === 'dsh') {
    const { Context } = await load<typeof import('@deepseek-ai/cordis')>('@deepseek-ai/cordis');
    const { default: AgentRegistry } = await load<typeof import('@deepseek-ai/dsh-agent')>('@deepseek-ai/dsh-agent');
    const { default: AgentLoop } = await load<typeof import('@deepseek-ai/dsh-agent-loop')>('@deepseek-ai/dsh-agent-loop');
    const { default: LlmRuntime, LlmAdapter, MessageId, freezeMessage } = await load<typeof import('@deepseek-ai/dsh-llm')>('@deepseek-ai/dsh-llm');
    const { default: SessionStore, SessionId } = await load<typeof import('@deepseek-ai/dsh-session')>('@deepseek-ai/dsh-session');
    const { default: Projection } = await load<typeof import('@deepseek-ai/dsh-session-projection')>('@deepseek-ai/dsh-session-projection');
    const { default: Attachment } = await load<typeof import('@deepseek-ai/dsh-attachment-local')>('@deepseek-ai/dsh-attachment-local');
    const { default: ToolRuntime } = await load<typeof import('@deepseek-ai/dsh-tools')>('@deepseek-ai/dsh-tools');
    const { default: SystemPrompt } = await load<typeof import('@deepseek-ai/dsh-system-prompt')>('@deepseek-ai/dsh-system-prompt');
    const persistence = await load<typeof import('@deepseek-ai/dsh-session-persistence-jsonl')>('@deepseek-ai/dsh-session-persistence-jsonl');
    const { installMemory } = await load<typeof import('../src/dsh.ts')>('@evernook/harnesses/dsh');
    class Synthetic extends LlmAdapter {
      async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
        entered(options.messages);
        if (blocked) await holdWriter();
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text: expected };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: expected } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
    const ctx = new Context(); cleanups.push(() => ctx.fiber.dispose());
    await ctx.plugin(SessionStore).await(); await ctx.plugin(Projection).await();
    await ctx.plugin(AgentRegistry).await(); await ctx.plugin(LlmRuntime).await();
    await ctx.plugin(Attachment, { dshHome: join(output, 'home') }).await();
    await ctx.plugin(ToolRuntime).await(); await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
    await ctx.plugin(persistence, { root: join(output, 'sessions'), compression: 'none' }).await();
    await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 }).await();
    ctx.llm.registerAdapter(['installed-synthetic'], new Synthetic());
    const memory = await installMemory(ctx, config); cleanups.push(() => memory.close());
    const sid = SessionId('synthetic-installed-smoke' + (trial ? '-' + trial : ''));
    const handle = await ctx.agents.create({ sessionId: sid, meta: { cwd: output, delegationDepth: 0 },
      agentOptions: { provider: 'installed-synthetic', model: 'synthetic' } });
    cleanups.push(() => handle.dispose());
    sources = async () => (await memory.streams(sid)).streams;
    ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') publish(frame.chunk.text); });
    ctx.on('agent/error', () => { error = 'native_agent_error'; });
    handle.agent.followup(freezeMessage({ id: MessageId('synthetic-installed-input'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '37.50' }] }));
    await handle.agent.whenIdle();
  } else {
    const { createProvider, createAssistantMessageEventStream } = await load<typeof import('@earendil-works/pi-ai')>('@earendil-works/pi-ai');
    const { ModelRuntime, SettingsManager, DefaultResourceLoader, createAgentSession, SessionManager } =
      await load<typeof import('@earendil-works/pi-coding-agent')>('@earendil-works/pi-coding-agent');
    const { installPiMemory } = await load<typeof import('../src/pi.ts')>('@evernook/harnesses/pi');
    const target: Model<Api> = { id: 'synthetic', name: 'Installed synthetic lifecycle probe', provider: 'installed-synthetic',
      api: 'installed-synthetic-v1', baseUrl: '', reasoning: false, input: ['text'], contextWindow: 32768, maxTokens: 256,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    const runtime = await ModelRuntime.create({ authPath: join(output, 'auth.json'), modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    const stream = (_model: Model<Api>, context: TranscriptContext) => {
      entered(context);
      const result = createAssistantMessageEventStream();
      const answer: AssistantMessage = { role: 'assistant', api: target.api, provider: target.provider, model: target.id,
        content: [{ type: 'text', text: expected }], timestamp: Date.now(), stopReason: 'stop',
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      void (async () => {
        if (blocked) await holdWriter();
        result.push({ type: 'start', partial: structuredClone(answer) });
        result.push({ type: 'text_start', contentIndex: 0, partial: structuredClone(answer) });
        result.push({ type: 'text_delta', contentIndex: 0, delta: expected, partial: structuredClone(answer) });
        result.push({ type: 'text_end', contentIndex: 0, content: expected, partial: structuredClone(answer) });
        result.push({ type: 'done', reason: 'stop', message: answer }); result.end(answer);
      })().catch(() => {
        const failed: AssistantMessage = { ...answer, content: [], stopReason: 'error', errorMessage: 'installed_provider_failed' };
        result.push({ type: 'error', reason: 'error', error: failed }); result.end(failed);
      });
      return result;
    };
    runtime.registerNativeProvider(createProvider({ id: target.provider, name: 'Installed synthetic', models: [target],
      auth: { apiKey: { name: 'No credentials', resolve: async () => ({ auth: {} }) } }, api: { stream, streamSimple: stream } }));
    const settings = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
    let memory: Awaited<ReturnType<typeof installPiMemory>> | undefined;
    const resources = new DefaultResourceLoader({ cwd: output, agentDir: join(output, 'agent'), settingsManager: settings,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      extensionFactories: [async pi => { memory = await installPiMemory(pi, { ...config, target });
        cleanups.push(() => memory?.close()); }] });
    await resources.reload();
    if (resources.getExtensions().errors.length) throw new Error('installed_extension_load_failed');
    const { session } = await createAgentSession({ cwd: output, agentDir: join(output, 'agent'), modelRuntime: runtime,
      resourceLoader: resources, settingsManager: settings, noTools: 'all',
      model: { ...target, provider: 'evernook-memory', api: 'evernook-durable-pi-v1' },
      sessionManager: SessionManager.create(output, join(output, 'sessions')) });
    cleanups.unshift(() => session.dispose());
    sources = async () => (await memory!.streams(session.sessionManager.getSessionId())).streams;
    session.subscribe(event => { if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') publish(event.assistantMessageEvent.delta); });
    await session.prompt('37.50'); error = memory!.status(session.sessionManager.getSessionId()).error_code ?? null;
  }
  if (blocked) {
    try { await reader.remember({ source: { entry, session: 'synthetic-storage-fault', branch: 'main', event: trial ?? 'probe' },
      role: 'user', sequence: 0, occurred_at: null, completion: 'complete', text: '合成写锁探针。' }); }
    catch (failure) {
      storageFailureCode = failure instanceof Error ? failure.message : 'unknown';
      storageFailureObserved = storageFailureCode === 'evernook:storage_busy';
    }
  } else {
    await Promise.all(firstProofs);
    const values = await sources!();
    if (values.length === 1) {
      const value = await reader.readStream(values[0]); replayMatches = value.complete && value.text === visible;
    }
  }
} catch (failure) { error = failure instanceof Error ? failure.message.slice(0, 200) : 'native_installation_failed'; }
finally {
  if (lock) {
    lock.stdin.end();
    if (lock.exitCode === null) await once(lock, 'exit', { signal: AbortSignal.timeout(10000) });
  }
  for (const cleanup of cleanups.reverse()) { try { await cleanup(); } catch { error ??= 'installed_cleanup_failed'; } }
  await reader.close();
}
const passed = (blocked ? error !== null && visible === '' && lockReady && storageFailureObserved && observedEvidenceId
  : error === null && visible === expected && observedEvidenceId && replayMatches && firstPrefixMatches) && networkAttempts === 0;
await writeFile(join(output, 'report.json'), JSON.stringify({ entry, imports, passed, error, visible,
  evidence_id_observed: observedEvidenceId, independent_first_prefix: firstPrefixMatches, independent_final_replay: replayMatches,
  network_attempts: networkAttempts, mode: mode ?? 'normal', lock_ready: lockReady,
  storage_write_failure_observed: storageFailureObserved, storage_write_failure_code: storageFailureCode,
  failed_commit_blocked_publication: blocked && passed,
  scope: 'Actual installed npm adapter/SDK and Python wheel, deterministic synthetic provider, legacy context mode. Normal prefix proof is eventual readback; lock mode tests no publication on failed commit. Not real-model quality, target-token admission, desktop UI or physical OS recovery.' }, null, 2) + '\n');
process.exitCode = passed ? 0 : 1;
