// Disposable synthetic-only Cordis bundle. Not included in the EverNook package.
import { randomUUID } from 'node:crypto';
import { readFileSync, realpathSync, mkdtempSync, writeFileSync } from 'node:fs';
import { basename, join, isAbsolute } from 'node:path';
import z from '@deepseek-ai/schemastery';
import { LlmAdapter, ToolCallId, MessageId, freezeMessage, isAgentLoopRequest } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';

export const name = 'evernook-synthetic94';
export const inject = ['agents', 'agentPresets', 'llm', 'tools', 'connection', 'sessions', 'sessionPersistence'];
export const Config = z.object({ root: z.string(), plan: z.string() });

export async function apply(ctx, config) {
  if (!isAbsolute(config.root) || !isAbsolute(config.plan)) throw new Error('synthetic absolute paths required');
  const root = realpathSync.native(config.root);
  if (!basename(root).startsWith('evernook-publish94-')) throw new Error('synthetic-only external root required');
  const work = mkdtempSync(join(root, 'synthetic-work-'));
  writeFileSync(join(work, 'probe-read.txt'), '合成读取🙂\r\n37.50元。\r\n', { flag: 'wx' });
  const plans = new Map();
  let modelRequests = 0; let toolExecutions = 0;
  ctx.on('tools/execute', (exec, next) => { toolExecutions++; return next(); });
  class Synthetic extends LlmAdapter {
    async *stream(options) {
      modelRequests++;
      const plan = plans.get(String(options.sessionId));
      if (!plan) throw new Error('missing synthetic session plan');
      plan.trace.push({ purpose: options.purpose, agent_loop: isAgentLoopRequest(options), tool_count: options.tools?.length ?? 0,
        recovery_context: JSON.stringify(options).includes('untrusted_read_only_recovery') });
      if (!isAgentLoopRequest(options)) {
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text: 'Synthetic auxiliary result' };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Synthetic auxiliary result' } };
        yield { type: 'finish', reason: { kind: 'stop' } }; return;
      }
      plan.requests++;
      const step = plan.steps[plan.requests - 1];
      if (step) {
        let offset = 0;
        if (plan.reasoning) {
          const before = plan.liveReasoning;
          yield { type: 'block-start', index: 0, blockType: 'reasoning' };
          yield { type: 'reasoning-delta', index: 0, text: plan.reasoning };
          yield { type: 'block-end', index: 0, block: { type: 'reasoning', text: plan.reasoning } };
          plan.reasoningHeld = plan.liveReasoning === before;
          offset = 1;
        }
        if (plan.preface) {
          yield { type: 'block-start', index: offset, blockType: 'text' };
          yield { type: 'text-delta', index: offset, text: plan.preface };
          plan.livePrefixObserved = plan.liveText.includes(plan.preface);
          yield { type: 'block-end', index: offset, block: { type: 'text', text: plan.preface } };
        }
        const index = offset + (plan.preface ? 1 : 0);
        const id = ToolCallId('synthetic-native-call-' + plan.requests);
        const args = JSON.stringify(step.args);
        yield { type: 'block-start', index, blockType: 'tool-call' };
        yield { type: 'tool-call-delta', index, id, name: step.tool, argumentsDelta: args };
        yield { type: 'block-end', index, block: { type: 'tool-call', id, name: step.tool, arguments: args } };
        yield { type: 'finish', reason: { kind: 'tool-calls' } };
      } else {
        const text = '合成工具验证完成。';
        yield { type: 'block-start', index: 0, blockType: 'text' };
        yield { type: 'text-delta', index: 0, text };
        yield { type: 'block-end', index: 0, block: { type: 'text', text } };
        yield { type: 'finish', reason: { kind: 'stop' } };
      }
    }
  }
  ctx.llm.registerAdapter(['synthetic-native94'], new Synthetic());
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const plan = plans.get(String(agent.session.id));
    if (plan && frame.type === 'chunk' && frame.chunk.type === 'text-delta') plan.liveText += frame.chunk.text;
    if (plan && frame.type === 'chunk' && frame.chunk.type === 'reasoning-delta') plan.liveReasoning += frame.chunk.text;
  });
  const json = (value, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
  function substitute(value) {
    if (typeof value === 'string') return value.replaceAll('$WORK_JSON', JSON.stringify(work.replaceAll('\\', '/'))).replaceAll('$WORK', work.replaceAll('\\', '/'));
    if (Array.isArray(value)) return value.map(substitute);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item)]));
    return value;
  }
  let busy = false;
  const remove = ctx.get('connection').fetch.register({ path: '/api/evernook-synthetic94/probe', methods: ['POST'], requestBody: 'streaming',
    async fetch(request) {
      let origin;
      try { origin = new URL(request.headers.get('origin')); } catch { return json({ error: 'forbidden' }, 403); }
      if (origin.hostname !== '127.0.0.1' || origin.host !== request.headers.get('host')
          || request.headers.get('content-type') !== 'application/json') return json({ error: 'forbidden' }, 403);
      const body = await request.text();
      if (Buffer.byteLength(body) > 4096) return json({ error: 'oversized' }, 413);
      const input = JSON.parse(body);
      if (input.action === 'recovery_counters') return json({ synthetic: true, model_requests: modelRequests, tool_executions: toolExecutions });
      if (input.action === 'inventory') {
        const presets = ctx.get('agentPresets');
        const scope = await presets?.acquireScope();
        try { return json({ synthetic: true, presets: presets ? await presets.list() : [],
          tools: ctx.tools.schemas(scope?.key).map(value => ({ name: value.name, parameters: value.parameters })) }); }
        finally { await scope?.[Symbol.asyncDispose](); }
      }
      const selected = JSON.parse(readFileSync(config.plan, 'utf8'));
      if (selected.synthetic !== true || typeof input.case !== 'string' || !Object.hasOwn(selected.cases, input.case)) return json({ error: 'unknown_synthetic_case' }, 400);
      if (busy) return json({ error: 'busy' }, 409);
      busy = true;
      let handle;
      let timer;
      let removeErrors;
      let removeMode;
      let removeExecute;
      let removeDeny;
      let removePublication;
      let cancelTimer;
      let timedOut = false;
      try {
        const entry = selected.cases[input.case];
        if (entry.preface !== undefined && (typeof entry.preface !== 'string' || entry.preface.length > 4096)) return json({ error: 'invalid_synthetic_preface' }, 400);
        if (entry.reasoning !== undefined && (typeof entry.reasoning !== 'string' || entry.reasoning.length > 4096)) return json({ error: 'invalid_synthetic_reasoning' }, 400);
        const steps = entry.steps ?? [{ tool: entry.tool, args: entry.args }];
        if (!Array.isArray(steps) || !steps.length || steps.length > 8
            || steps.some(step => !['read', 'write', 'edit', 'pwsh', 'run_code'].includes(step.tool))) return json({ error: 'unsupported_synthetic_tool' }, 400);
        const setup = async agentCtx => { await ctx.get('agentPresets')?.mount(agentCtx, entry.preset); };
        const agentOptions = { provider: 'synthetic-native94', model: 'local-test' };
        if (entry.resumeSession !== undefined) {
          if (!/^evernook-recovery-[0-9a-f]{40}$/.test(entry.resumeSession)) return json({ error: 'synthetic_readonly_target_required' }, 400);
          const saved = await ctx.sessionPersistence.open(SessionId(entry.resumeSession), 'read');
          try {
            if (!(await saved.read()).events.some(event => event.type === 'plugin:dsh-evernook/recovery-readonly')) {
              return json({ error: 'synthetic_readonly_policy_required' }, 400);
            }
          } finally { await saved.close(); }
          handle = await ctx.agents.resume({ resumeSessionId: SessionId(entry.resumeSession), setup, agentOptions });
        } else handle = await ctx.agents.create({ sessionId: SessionId(randomUUID()), meta: { cwd: work, delegationDepth: 0 }, setup, agentOptions });
        const id = String(handle.agent.session.id);
        const plan = { steps: substitute(steps), requests: 0, trace: [], preface: entry.preface, liveText: '', livePrefixObserved: false,
          reasoning: entry.reasoning, liveReasoning: '', reasoningHeld: false };
        plans.set(id, plan);
        removeMode = handle.agent.ctx.tools.presentAs(entry.mode ?? 'native');
        const errors = [];
        removeErrors = ctx.on('agent/error', ({ agent, error }) => { if (String(agent.id) === String(handle.agent.id)) errors.push(error.message); });
        if (entry.blockPublication) removePublication = ctx.on('evernook/tools/before-publish', async exec => {
          if (String(exec.agent?.session.id) === id) throw new Error('synthetic publication failure after saving');
        });
        if (entry.cancelAfterMs) removeExecute = ctx.on('tools/execute', async (exec, next) => {
          if (String(exec.agent?.session.id) === id) cancelTimer = setTimeout(() => handle.agent.cancel({ kind: 'user' }), entry.cancelAfterMs);
          return next();
        });
        if (entry.deny) removeDeny = ctx.on('tools/pre-execute', (exec, next) => String(exec.agent?.session.id) === id
          ? { kind: 'deny', reason: 'synthetic owner denied this operation' } : next());
        timer = setTimeout(() => { timedOut = true; handle.agent.cancel({ kind: 'user' }); }, 15000);
        handle.agent.followup(freezeMessage({ id: MessageId(randomUUID()), role: 'user', source: { kind: 'user' },
          content: [{ type: 'text', text: '合成验收：只执行受信计划中的临时目录工具，不使用真实模型或用户数据。' }] }));
        await handle.agent.whenIdle();
        await ctx.sessions.flush(handle.agent.session);
        const events = handle.agent.session.snapshotEvents();
        plans.delete(id);
        return json({ synthetic: true, case: input.case, session: id, workspace: work, requests: plan.requests, timed_out: timedOut, errors,
          expected_preface: entry.preface, live_prefix_observed: plan.livePrefixObserved,
          expected_reasoning: entry.reasoning?.repeat(steps.length), live_reasoning: plan.liveReasoning, reasoning_held: plan.reasoningHeld,
          trace: plan.trace, event_types: [...new Set(events.map(value => value.type))],
          expected_error: entry.expectError === true, expected_text: entry.expectText, expected_file: entry.expectedFile,
          expected_readonly: entry.resumeSession !== undefined,
          expected_requests: entry.cancelAfterMs || entry.blockPublication ? 1 : steps.length + 1,
          calls: events.filter(value => value.type === 'tool/call' || value.type === 'tool/ptc-dispatch-start').map(value => ({ type: value.type, seq: value.seq, data: value.data })),
          results: events.filter(value => value.type === 'tool/result' || value.type === 'tool/ptc-dispatch').map(value => ({ type: value.type, data: value.data })),
          blocked: events.filter(value => value.type === 'plugin:dsh-evernook/publication-blocked').map(value => value.data) });
      } catch (error) { return json({ synthetic: true, error: error.message }, 500); }
      finally { clearTimeout(timer); clearTimeout(cancelTimer); removeErrors?.(); removeExecute?.(); removeDeny?.(); removePublication?.(); removeMode?.(); await handle?.dispose(); busy = false; }
    },
  });
  ctx.effect(() => remove);
}
