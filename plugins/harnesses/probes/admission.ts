import { createHash } from 'node:crypto';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { Socket } from 'node:net';
import { Context } from '@deepseek-ai/cordis';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import LocalAttachmentStore from '@deepseek-ai/dsh-attachment-local';
import LlmRuntime, { LlmAdapter, MessageId, freezeMessage, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session';
import * as persistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import { createAssistantMessageEventStream, createProvider, type Api, type AssistantMessage, type Model, type SimpleStreamOptions, type TranscriptContext } from '@earendil-works/pi-ai';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { installMemory } from '../src/dsh.ts';
import { installPiMemory } from '../src/pi.ts';
import { MemoryClient, type Source } from '../src/client.ts';
import { LocalQwenChat, type ModelRun } from './local-qwen.ts';

const [entry, planFile, output, modelProfile, embeddingConfig, python, maximum, control] = process.argv.slice(2);
if (!['dsh', 'pi'].includes(entry) || ![planFile, output, modelProfile, embeddingConfig, python].every(Boolean)) {
  throw new Error('Use: admission.ts dsh|pi plan.json new-external-output model-profile.json embedding-profile.json python.exe [max-cases]');
}
if (control && (control !== 'cancel-after-first-visible' || entry !== 'dsh')) throw new Error('unsupported_diagnostic_control');
const checkout = resolve(import.meta.dirname, '../../..');
const destination = resolve(output);
if (destination === checkout || destination.startsWith(checkout + '\\')) throw new Error('external_output_required');
await mkdir(destination);
const policyFile = join(checkout, 'probes/admission/policy.json');
const policyText = await readFile(policyFile, 'utf8');
const policy = JSON.parse(policyText);
const plan = JSON.parse(await readFile(planFile, 'utf8')) as { suite_hash: string; entry: string; plans: Plan[] };
if (plan.suite_hash !== policy.suite_hash || plan.entry !== entry || process.versions.node !== policy.environment.node) throw new Error('experiment_identity_mismatch');
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const model = await LocalQwenChat.create(modelProfile);
const profile = JSON.parse(await readFile(modelProfile, 'utf8')) as { model_dir: string };
const digest = (text: string | Buffer) => createHash('sha256').update(text).digest('hex');
const sourceHashes: Record<string, string> = {};
for (const name of await readdir(join(checkout, 'src/evernook'))) if (/\.(py|mjs)$/.test(name)) {
  sourceHashes[name] = digest(await readFile(join(checkout, 'src/evernook', name)));
}
await writeFile(join(destination, 'manifest.json'), JSON.stringify({ phase: 'actual_sdk_local_model_quality', entry,
  suite_hash: plan.suite_hash, policy_sha256: digest(policyText), plan_sha256: digest(await readFile(planFile)),
  probe_sha256: digest(await readFile(import.meta.filename)), model_probe_sha256: digest(await readFile(join(import.meta.dirname, 'local-qwen.ts'))),
  file_access_probe_sha256: digest(await readFile(join(import.meta.dirname, 'model-file-access.ts'))),
  core_sha256: sourceHashes, model: model.startup, model_preloaded_before_user_requests: true,
  parser: policy.host.parser, sdk: { dsh: '0.2.1-rc.3', pi: '1.4.0' }, node: process.versions.node,
  maximum_cases: maximum ? Number(maximum) : null, diagnostic_control: control ?? null,
  scope: 'Independent allowed-source fixture per question; explicit facts only, no automatic extraction claim.' }, null, 2) + '\n');

interface Plan {
  query_id: string; category: string; question: string; config: string; access: string;
  source_aliases: Record<string, string>; fact_keys: [string, string][];
}
interface Observation {
  id: string; payload: Record<string, unknown>;
}
interface CaseRun {
  query_id: string; entry: string; category: string; visible: string; model_runs: ModelRun[];
  requests: unknown[]; full_request_tokens: number[]; host_boot_ms: number | null; request_ms: number | null;
  memory_pre_provider_ms: number | null; first_reliable_visible_ms: number | null;
  first_visible_independently_read: boolean | null; final_replay_matches: boolean | null;
  error_code: string | null; context: unknown;
  diagnostics?: { name: string; message: string; stack?: string }[];
  cancellation?: { requested: boolean; state: string; final_message_id: string | null; verified: boolean };
}

async function counterConfig(row: Plan) {
  const root = resolve(row.config, '..');
  const tokenizer = join(profile.model_dir, 'tokenizer.json');
  const tokenizerConfig = join(profile.model_dir, 'tokenizer_config.json');
  const counter = join(root, 'qwen-counter.json');
  await writeFile(counter, JSON.stringify({ schema_version: 1, node: process.execPath,
    package_dir: resolve(import.meta.dirname, '../node_modules/@huggingface/tokenizers'), model_id: model.id,
    tokenizer_file: tokenizer, config_file: tokenizerConfig,
    tokenizer_sha256: digest(await readFile(tokenizer)), config_sha256: digest(await readFile(tokenizerConfig)) }));
  const path = join(root, 'host-context.json');
  await writeFile(path, JSON.stringify({ schema_version: 1, tokenizer_config: counter,
    policy: { normal_tokens: policy.host.memory_tokens, deep_tokens: policy.host.memory_tokens * 2, pinned_tokens: 512,
      recent_days: 7, recent_target_chars: 8192, fact_keys: row.fact_keys } }));
  return path;
}

async function replay(reader: MemoryClient, source: Source) {
  let text = '';
  let cursor = 0;
  for (let page = 0; page < 128; page++) {
    const value = await reader.readStream(source, cursor);
    text += value.text;
    if (value.next_cursor === null) return { text, complete: value.complete, state: value.state, final_message_id: value.final_message_id };
    cursor = value.next_cursor;
  }
  throw new Error('replay_limit');
}

function payload(run: CaseRun, aliases: Record<string, string>): Record<string, unknown> {
  if (run.cancellation) return { error_code: run.error_code
    ?? (run.cancellation.verified ? 'diagnostic_canceled' : 'cancellation_not_verified'), observed: run.visible };
  if (run.error_code) return { error_code: run.error_code, observed: run.visible };
  try {
    const result = JSON.parse(run.visible);
    if (!result || typeof result !== 'object' || Array.isArray(result)
        || Object.keys(result).sort().join(',') !== 'answer,decision,source_ids'
        || !['answer', 'abstain', 'clarify', 'refuse'].includes(result.decision)
        || (result.answer !== null && typeof result.answer !== 'string') || !Array.isArray(result.source_ids)
        || !result.source_ids.every((id: unknown) => typeof id === 'string')) throw new Error('format');
    return { decision: result.decision, answer: result.answer,
      source_ids: result.source_ids.map((id: string) => aliases[id] ?? id) };
  } catch { return { error_code: 'invalid_visible_model_json', observed: run.visible }; }
}

async function runCase(row: Plan): Promise<CaseRun> {
  const run: CaseRun = { query_id: row.query_id, entry, category: row.category, visible: '', model_runs: [], requests: [],
    full_request_tokens: [], host_boot_ms: null, request_ms: null, memory_pre_provider_ms: null,
    first_reliable_visible_ms: null, first_visible_independently_read: null, final_replay_matches: null, error_code: null, context: null };
  const bootAt = performance.now();
  await mkdir(join(destination, row.query_id));
  let reader: MemoryClient | undefined;
  const cleanups: (() => unknown | Promise<unknown>)[] = [];
  let requestedAt = 0;
  const proofs: Promise<void>[] = [];
  let streamSource: (() => Promise<Source[]>) | undefined;
  let cancelTarget: (() => void) | undefined;
  let cancellationRequested = false;
  const publish = (text: string) => {
    if (!text) return;
    run.visible += text;
    run.first_reliable_visible_ms ??= performance.now() - requestedAt;
    if (proofs.length || !streamSource) return;
    const first = run.visible;
    proofs.push((async () => {
      const sources = await streamSource!();
      if (sources.length !== 1) { run.first_visible_independently_read = false; return; }
      run.first_visible_independently_read = (await replay(reader!, sources[0])).text.startsWith(first);
    })());
    if (control && cancelTarget) { cancellationRequested = true; cancelTarget(); }
  };
  const entered = (request: unknown) => {
    run.memory_pre_provider_ms ??= performance.now() - requestedAt;
    run.requests.push(structuredClone(request));
  };
  try {
    const contextConfig = await counterConfig(row);
    const config = { python, config: row.config, access: row.access, entry, cwd: checkout, contextConfig, embeddingConfig,
      streamMode: 'durable_text' as const };
    reader = new MemoryClient(config);
    await reader.connect();
    if (entry === 'dsh') {
      class ActualModel extends LlmAdapter {
        override async resolveModel(provider: string, id: string, signal?: AbortSignal) {
          return { ...await super.resolveModel(provider, id, signal), context: { contextWindow: model.contextWindow } };
        }
        async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
          const request = { messages: options.messages, system: options.system, tools: options.tools };
          entered(request);
          let text = '';
          yield { type: 'block-start', index: 0, blockType: 'text' };
          for await (const delta of model.stream(request, options.maxTokens ?? policy.host.output_reserve_tokens, options.signal)) {
            text += delta; yield { type: 'text-delta', index: 0, text: delta };
          }
          if (model.last) run.model_runs.push(structuredClone(model.last));
          yield { type: 'block-end', index: 0, block: { type: 'text', text } };
          yield { type: 'finish', reason: { kind: model.last?.finished === 'length' ? 'max-tokens' : 'stop' } };
        }
      }
      const ctx = new Context();
      cleanups.push(() => ctx.fiber.dispose());
      await ctx.plugin(SessionStore).await(); await ctx.plugin(SessionProjectionRegistry).await();
      await ctx.plugin(AgentRegistry).await(); await ctx.plugin(LlmRuntime).await();
      await ctx.plugin(LocalAttachmentStore, { dshHome: join(destination, row.query_id, 'dsh-home') }).await();
      await ctx.plugin(ToolRuntime).await(); await ctx.plugin(SystemPrompt, { includeHarnessIdentity: false }).await();
      await ctx.plugin(persistence, { root: join(destination, row.query_id, 'sessions'), compression: 'none' }).await();
      await ctx.plugin(AgentLoop, { agents: [], maxParallelToolCalls: 1 }).await();
      ctx.llm.registerAdapter(['local-qwen-evaluation'], new ActualModel());
      const memory = await installMemory(ctx, { ...config, memoryBudget: { provider: 'local-qwen-evaluation', modelId: model.id,
        memoryTokens: policy.host.memory_tokens, contextWindow: model.contextWindow, outputReserveTokens: policy.host.output_reserve_tokens,
        countRequest: request => { const count = model.count({ messages: request.messages, system: request.system, tools: request.tools });
          run.full_request_tokens.push(count); return count; } } });
      cleanups.push(() => memory.close());
      const sid = SessionId('evaluation-' + row.query_id);
      const handle = await ctx.agents.create({ sessionId: sid, meta: { cwd: join(destination, row.query_id), delegationDepth: 0 },
        agentOptions: { provider: 'local-qwen-evaluation', model: model.id } });
      cleanups.push(() => handle.dispose());
      cancelTarget = () => handle.agent.cancel({ kind: 'user' });
      streamSource = async () => (await memory.streams(sid)).streams;
      ctx.on('agent/assistant-stream', ({ frame }) => { if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') publish(frame.chunk.text); });
      ctx.on('agent/error', ({ error }) => { run.error_code = 'native_agent_error';
        if (error instanceof Error) (run.diagnostics ??= []).push({ name: error.name, message: error.message.slice(0, 1024), stack: error.stack?.slice(0, 4096) }); });
      run.host_boot_ms = performance.now() - bootAt;
        requestedAt = performance.now();
        handle.agent.followup(freezeMessage({ id: MessageId('evaluation-' + row.query_id), role: 'user', source: { kind: 'user' },
          content: [{ type: 'text', text: row.question }] }));
        await handle.agent.whenIdle();
        run.context = memory.status(sid);
        await Promise.all(proofs);
        const sources = await streamSource();
        if (sources.length === 1) { const result = await replay(reader, sources[0]); run.final_replay_matches = result.text === run.visible;
          if (control) {
            const verified = cancellationRequested && result.state === 'canceled' && !result.complete
              && result.final_message_id === null && run.final_replay_matches && run.first_visible_independently_read === true
              && model.last?.failure?.aborted === true;
            run.cancellation = { requested: cancellationRequested, state: result.state, final_message_id: result.final_message_id, verified };
            if (!verified) run.error_code ??= 'cancellation_not_verified';
          } else if (!result.complete) run.error_code ??= 'incomplete_model_output'; }
    } else {
      const target: Model<Api> = { id: model.id, name: 'Local pinned Qwen evaluation candidate', provider: 'local-qwen-evaluation',
        api: 'local-qwen-evaluation-v1', baseUrl: '', reasoning: false, input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: model.contextWindow, maxTokens: policy.host.output_reserve_tokens };
      const runtime = await ModelRuntime.create({ authPath: join(destination, row.query_id, 'pi-auth.json'), modelsPath: null,
        allowModelNetwork: false, refreshOnCreate: false });
      function stream(_model: Model<Api>, context: TranscriptContext, options?: SimpleStreamOptions) {
        const output = createAssistantMessageEventStream();
        void (async () => {
          entered(context);
          const answer: AssistantMessage = { role: 'assistant', api: target.api, provider: target.provider, model: target.id, content: [],
            timestamp: Date.now(), stopReason: 'stop', usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
              cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
          output.push({ type: 'start', partial: structuredClone(answer) });
          output.push({ type: 'text_start', contentIndex: 0, partial: structuredClone(answer) });
          let text = '';
          for await (const delta of model.stream(context, options?.maxTokens ?? policy.host.output_reserve_tokens, options?.signal)) {
            text += delta; answer.content = [{ type: 'text', text }];
            output.push({ type: 'text_delta', contentIndex: 0, delta, partial: structuredClone(answer) });
          }
          if (model.last) {
            run.model_runs.push(structuredClone(model.last));
            answer.usage.input = model.last.input_tokens; answer.usage.output = model.last.generated_token_ids.length;
            answer.usage.totalTokens = answer.usage.input + answer.usage.output; answer.stopReason = model.last.finished;
          }
          answer.content = [{ type: 'text', text }];
          output.push({ type: 'text_end', contentIndex: 0, content: text, partial: structuredClone(answer) });
          output.push({ type: 'done', reason: answer.stopReason === 'length' ? 'length' : 'stop', message: answer }); output.end(answer);
        })().catch(() => {
          const input = model.last?.input_tokens ?? 0;
          const generated = model.last?.generated_token_ids.length ?? 0;
          const error: AssistantMessage = { role: 'assistant', api: target.api, provider: target.provider, model: target.id,
            content: [], timestamp: Date.now(), stopReason: 'error', errorMessage: 'local_evaluation_failed',
            usage: { input, output: generated, cacheRead: 0, cacheWrite: 0, totalTokens: input + generated,
              cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
          output.push({ type: 'error', reason: 'error', error }); output.end(error);
        });
        return output;
      }
      runtime.registerNativeProvider(createProvider({ id: target.provider, name: 'Offline evaluation', models: [target],
        auth: { apiKey: { name: 'Local offline evaluation', resolve: async () => ({ auth: {} }) } }, api: { stream, streamSimple: stream } }));
      const settings = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
      let memory: Awaited<ReturnType<typeof installPiMemory>> | undefined;
      const resources = new DefaultResourceLoader({ cwd: destination, agentDir: join(destination, row.query_id, 'pi-agent'), settingsManager: settings,
        noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
        extensionFactories: [async pi => { memory = await installPiMemory(pi, { ...config, target,
          memoryBudget: { provider: target.provider, modelId: target.id, memoryTokens: policy.host.memory_tokens,
            contextWindow: model.contextWindow, outputReserveTokens: policy.host.output_reserve_tokens,
            countRequest: request => { const count = model.count(request.context); run.full_request_tokens.push(count); return count; } } });
          cleanups.push(() => memory?.close()); }] });
      await resources.reload();
      if (resources.getExtensions().errors.length) throw new Error('extension_load_failed');
      const { session } = await createAgentSession({ cwd: destination, agentDir: join(destination, row.query_id, 'pi-agent'), modelRuntime: runtime,
        resourceLoader: resources, settingsManager: settings, noTools: 'all', model: { ...target, provider: 'evernook-memory', api: 'evernook-durable-pi-v1' },
        sessionManager: SessionManager.create(destination, join(destination, row.query_id, 'pi-sessions')) });
      cleanups.unshift(() => session.dispose());
      const sid = session.sessionManager.getSessionId();
      streamSource = async () => (await memory!.streams(sid)).streams;
      session.subscribe(event => { if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') publish(event.assistantMessageEvent.delta); });
      run.host_boot_ms = performance.now() - bootAt;
        requestedAt = performance.now(); await session.prompt(row.question); run.context = memory!.status(sid);
        run.error_code = memory!.status(sid).error_code ?? null;
        await Promise.all(proofs);
        const sources = await streamSource();
        if (sources.length === 1) { const result = await replay(reader, sources[0]); run.final_replay_matches = result.text === run.visible;
          if (!result.complete) run.error_code ??= 'incomplete_model_output'; }
    }
  } catch (error) { run.error_code ??= 'host_case_failed';
    if (error instanceof Error) (run.diagnostics ??= []).push({ name: error.name, message: error.message.slice(0, 1024), stack: error.stack?.slice(0, 4096) }); }
  finally {
    if (requestedAt) run.request_ms = performance.now() - requestedAt;
    for (const cleanup of cleanups.reverse()) {
      try { await cleanup(); } catch (error) {
        run.error_code ??= 'host_cleanup_failed';
        if (error instanceof Error) (run.diagnostics ??= []).push({ name: error.name, message: error.message.slice(0, 1024) });
      }
    }
    await reader?.close();
  }
  if (!run.requests.length) run.error_code ??= 'target_not_dispatched';
  if (control && !run.cancellation) {
    run.cancellation = { requested: cancellationRequested, state: 'unverified', final_message_id: null, verified: false };
    run.error_code ??= 'cancellation_not_observed';
  }
  if (run.requests.length === 1 && !run.model_runs.length && model.last?.request_sha256 === digest(JSON.stringify(run.requests[0]))) {
    run.model_runs.push(structuredClone(model.last));
  }
  if (run.requests.length > 1) run.error_code ??= 'multiple_model_attempts';
  if (run.visible && (run.final_replay_matches !== true || run.first_visible_independently_read !== true)) run.error_code ??= 'publication_not_verified';
  return run;
}

const rows: Observation[] = [];
const runs: CaseRun[] = [];
try {
  const selected = maximum ? plan.plans.slice(0, Number(maximum)) : plan.plans;
  for (const row of selected) {
    const run = await runCase(row);
    runs.push(run); rows.push({ id: 'host-' + row.query_id, payload: payload(run, row.source_aliases) });
    await writeFile(join(destination, 'observations.json'), JSON.stringify({ suite_hash: plan.suite_hash, rows }) + '\n');
    await writeFile(join(destination, 'runs.json'), JSON.stringify({ entry, network_attempts: networkAttempts, runs }) + '\n');
    console.log(JSON.stringify({ entry, query_id: row.query_id, completed: runs.length, error_code: run.error_code,
      memory_ms: run.memory_pre_provider_ms, first_reliable_visible_ms: run.first_reliable_visible_ms }));
  }
} finally { await model.close(); }
