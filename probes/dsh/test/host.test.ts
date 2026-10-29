import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { LlmAdapter, MessageId, ToolCallId, freezeMessage, isAgentLoopRequest, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { SessionId, SessionLogOffset, buildForkSeed } from '@deepseek-ai/dsh-session';
import { Evidence, assertExternalPath, openProbe, userInput } from '../src/host.ts';
import { fileURLToPath } from 'node:url';

async function syntheticRoot() {
  const root = await mkdtemp(join(process.env.EVERNOOK_PROBE_RUN_ROOT ?? tmpdir(), 'evernook-r0-'));
  console.log(`Synthetic host evidence: ${root}`);
  return root;
}

class InspectAdapter extends LlmAdapter {
  requests: GenerateOptions[] = [];
  inspect?: (options: GenerateOptions) => void | Promise<void>;
  async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    await this.inspect?.(options);
    this.requests.push(options);
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text: '合成回复。' };
    yield { type: 'block-end', index: 0, block: { type: 'text', text: '合成回复。' } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

test('real dsh submits evidence before the provider and injects it into a new session', async () => {
  const root = await syntheticRoot();
  const adapter = new InspectAdapter();
  const probe = await openProbe(root, adapter);
  try {
    adapter.inspect = () => {
      const independentReader = new Evidence(join(root, 'evidence.sqlite'));
      try {
        assert.match(JSON.stringify(independentReader.events('synthetic-a')), /合成项目预算 37 元/);
      } finally { independentReader.close(); }
    };
    const first = await probe.create(SessionId('synthetic-a'));
    first.agent.followup(userInput('synthetic-001', '请记住：合成项目预算 37 元。\n保留标点🙂'));
    await first.agent.whenIdle();
    assert.equal(adapter.requests.length, 1);
    assert.ok(isAgentLoopRequest(adapter.requests[0]));
    assert.ok(probe.receipts.some(r => r.stage === 'before-provider' && r.sessionId === 'synthetic-a'));
    await first.dispose();
    const second = await probe.create(SessionId('synthetic-b'));
    second.agent.followup(userInput('synthetic-002', '之前的预算是多少？'));
    await second.agent.whenIdle();
    assert.equal(adapter.requests.length, 2);
    assert.match(JSON.stringify(adapter.requests[1].messages), /合成项目预算 37 元/);
    assert.match(JSON.stringify(adapter.requests[1].messages), /synthetic-a/);
    await second.dispose();
  } finally {
    await probe.close();
  }
});

test('a rejected durability barrier prevents provider dispatch and a success receipt', async () => {
  const root = await syntheticRoot();
  const adapter = new InspectAdapter();
  const probe = await openProbe(root, adapter);
  const errors: unknown[] = [];
  probe.ctx.on('agent/error', ({ error }) => { errors.push(error); });
  const handle = await probe.create(SessionId('synthetic-flush-failure'));
  const removeFault = probe.ctx.on('session/flush', () => { throw new Error('synthetic flush failure'); });
  try {
    handle.agent.followup(userInput('failure-001', '合成数据，不应假成功。'));
    await handle.agent.whenIdle();
    assert.equal(adapter.requests.length, 0);
    assert.equal(probe.receipts.length, 0);
    assert.ok(errors.some(error => String(error).includes('synthetic flush failure')));
  } finally {
    removeFault();
    await handle.dispose();
    await probe.close();
  }
});

test('a real dsh fork preserves lineage and keeps branch-local source identities', async () => {
  const root = await syntheticRoot();
  const probe = await openProbe(root, new InspectAdapter());
  const parent = await probe.create(SessionId('synthetic-parent'));
  try {
    parent.agent.followup(userInput('parent-001', '合成父分支：预算 37 元。'));
    await parent.agent.whenIdle();
    const events = parent.agent.session.snapshotEvents();
    const child = await probe.ctx.agents.create({
      sessionId: SessionId('synthetic-child'),
      seed: buildForkSeed(events, events.at(-1)!.seq),
      inheritedEventCount: SessionLogOffset(events.length),
      meta: { cwd: root, delegationDepth: 0, parentSession: parent.agent.id, isSeeded: true },
      agentOptions: { provider: 'synthetic', model: 'deterministic' },
    });
    try {
      child.agent.followup(userInput('child-001', '合成子分支：预算改为 52 元。'));
      await child.agent.whenIdle();
      await probe.capture(child.agent.session, 'final');
      assert.equal(probe.evidence.source('synthetic-child')?.parentSession, 'synthetic-parent');
      assert.match(JSON.stringify(probe.evidence.events('synthetic-child')), /预算改为 52 元/);
      assert.doesNotMatch(JSON.stringify(probe.evidence.events('synthetic-parent')), /预算改为 52 元/);
      assert.ok(child.agent.session.inheritedEventCount > 0);
    } finally { await child.dispose(); }
  } finally {
    await parent.dispose();
    await probe.close();
  }
});

test('rescan from persisted dsh history is idempotent and restores a missed collection', async () => {
  const root = await syntheticRoot();
  let probe = await openProbe(root, new InspectAdapter());
  const handle = await probe.create(SessionId('synthetic-rescan'));
  handle.agent.followup(userInput('rescan-001', '合成记录：旧年编号 2048。'));
  await handle.agent.whenIdle();
  await probe.capture(handle.agent.session, 'final');
  const expected = handle.agent.session.snapshotEvents();
  const header = handle.agent.session.header;
  await handle.dispose();
  await probe.close();
  probe = await openProbe(root, new InspectAdapter());
  try {
    const freshSink = new Evidence(join(root, 'recovered.sqlite'));
    try {
      await probe.rescan(SessionId('synthetic-rescan'), freshSink);
      await probe.rescan(SessionId('synthetic-rescan'), freshSink);
      assert.deepEqual(freshSink.events('synthetic-rescan'), expected);
      assert.deepEqual(freshSink.source('synthetic-rescan'), header);
      const conflicting = structuredClone([...expected]);
      conflicting[0] = { ...conflicting[0], time: conflicting[0].time + 1 };
      assert.throws(() => freshSink.ingest(header, conflicting), /Source conflict/);
      assert.deepEqual(freshSink.events('synthetic-rescan'), expected);
    } finally { freshSink.close(); }
  } finally { await probe.close(); }
});

test('actual dsh tool dispatch preserves the call and local result in collected evidence', async () => {
  class ToolAdapter extends InspectAdapter {
    async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
      if (this.requests.length > 0) { yield* super.stream(options); return; }
      this.requests.push(options);
      const block = { type: 'tool-call' as const, id: ToolCallId('synthetic-tool-001'), name: 'synthetic_echo', arguments: '{}' };
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: '{}' };
      yield { type: 'block-end', index: 0, block };
      yield { type: 'finish', reason: { kind: 'tool-calls' } };
    }
  }
  const root = await syntheticRoot();
  const adapter = new ToolAdapter();
  const probe = await openProbe(root, adapter);
  probe.ctx.tools.register({
    name: 'synthetic_echo', description: 'Return a local synthetic constant', parameters: { type: 'object', properties: {} },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: String(value) }] },
    execute: async () => '合成工具结果：不访问网络。',
  });
  const handle = await probe.create(SessionId('synthetic-tool'));
  try {
    handle.agent.followup(userInput('tool-input-001', '运行合成工具。'));
    await handle.agent.whenIdle();
    await probe.capture(handle.agent.session, 'final');
    const events = probe.evidence.events('synthetic-tool');
    assert.equal(adapter.requests.length, 2);
    assert.ok(events.some(event => event.type === 'tool/call'));
    assert.ok(events.some(event => event.type === 'tool/result'));
    assert.match(JSON.stringify(events), /合成工具结果：不访问网络/);
  } finally { await handle.dispose(); await probe.close(); }
});

test('cooperative stream cancellation preserves the observed prefix with interrupted status', async () => {
  let cancel = () => {};
  class InterruptedAdapter extends LlmAdapter {
    async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
      yield { type: 'block-start', index: 0, blockType: 'text' };
      yield { type: 'text-delta', index: 0, text: '合成流前缀🙂' };
      cancel();
      options.signal?.throwIfAborted();
    }
  }
  const root = await syntheticRoot();
  const probe = await openProbe(root, new InterruptedAdapter());
  const handle = await probe.create(SessionId('synthetic-interrupted'));
  cancel = () => handle.agent.cancel({ kind: 'user' });
  try {
    handle.agent.followup(userInput('interrupted-001', '测试合成流中断。'));
    await handle.agent.whenIdle();
    await probe.capture(handle.agent.session, 'final');
    const events = probe.evidence.events('synthetic-interrupted');
    const assistant = events.find(event => event.type === 'assistant/message');
    assert.equal(assistant?.type, 'assistant/message');
    if (assistant?.type === 'assistant/message') {
      assert.equal(assistant.data.interrupted, true);
      assert.deepEqual(assistant.data.message.content, [{ type: 'text', text: '合成流前缀🙂' }]);
    }
  } finally { await handle.dispose(); await probe.close(); }
});

test('retrying a failed provider request does not duplicate the source user message', async () => {
  class RetryAdapter extends InspectAdapter {
    async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
      if (this.requests.length > 0) { yield* super.stream(options); return; }
      this.requests.push(options);
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'SYNTHETIC_TRANSIENT', message: 'synthetic retryable failure' } } };
    }
  }
  const root = await syntheticRoot();
  const adapter = new RetryAdapter();
  const probe = await openProbe(root, adapter);
  probe.ctx.on('agent/request-error', async (payload, next) => {
    if (payload.failure.code === 'SYNTHETIC_TRANSIENT') return { kind: 'retry' };
    return next();
  });
  const handle = await probe.create(SessionId('synthetic-retry'));
  try {
    handle.agent.followup(userInput('retry-001', '合成用户输入只能有一份来源。'));
    await handle.agent.whenIdle();
    await probe.capture(handle.agent.session, 'final');
    const events = probe.evidence.events('synthetic-retry');
    assert.equal(adapter.requests.length, 2);
    assert.equal(events.filter(event => event.type === 'user/message' && event.data.source.kind === 'user').length, 1);
    assert.equal(events.filter(event => event.type === 'assistant/attempt').length, 1);
    assert.equal(events.filter(event => event.type === 'assistant/message').length, 1);
  } finally { await handle.dispose(); await probe.close(); }
});

test('real dsh attachment bytes remain in the host store and evidence preserves their reference', async () => {
  const root = await syntheticRoot();
  const adapter = new InspectAdapter();
  let probe = await openProbe(root, adapter);
  const bytes = Buffer.from('合成附件原文\n🙂', 'utf8');
  const ref = await probe.ctx.attachments.saveFile({ data: bytes, name: 'synthetic.txt' });
  const handle = await probe.create(SessionId('synthetic-attachment'));
  try {
    handle.agent.followup(freezeMessage({
      id: MessageId('attachment-input-001'), role: 'user', source: { kind: 'user' },
      content: [{ type: 'file', attachment: ref }],
    }));
    await handle.agent.whenIdle();
    await probe.capture(handle.agent.session, 'final');
    const events = probe.evidence.events('synthetic-attachment');
    assert.match(JSON.stringify(events), new RegExp(ref.attachmentId));
    assert.equal(adapter.requests.length, 1);
    // Model-facing text handles are a different representation from retained file bytes.
    assert.match(JSON.stringify(adapter.requests[0].messages), /synthetic.txt/);
  } finally { await handle.dispose(); await probe.close(); }
  probe = await openProbe(root, new InspectAdapter());
  try {
    const chunks: Uint8Array[] = [];
    for await (const chunk of probe.ctx.attachments.readFileStream(ref)) chunks.push(chunk);
    assert.deepEqual(Buffer.concat(chunks), bytes);
    assert.doesNotMatch(probe.evidence.context(), /合成附件原文/);
  } finally { await probe.close(); }
});

test('observed native limitation: re-delivery after settlement executes twice, while rescan stays idempotent', async () => {
  const root = await syntheticRoot();
  const adapter = new InspectAdapter();
  const probe = await openProbe(root, adapter);
  const handle = await probe.create(SessionId('synthetic-redelivery'));
  try {
    const message = userInput('same-source-001', '合成重复投递：同一源消息。');
    handle.agent.followup(message);
    await handle.agent.whenIdle();
    handle.agent.followup(message);
    await handle.agent.whenIdle();
    await probe.capture(handle.agent.session, 'final');
    const original = probe.evidence.events('synthetic-redelivery');
    assert.equal(adapter.requests.length, 2, 'documents native behavior, not desired production idempotency');
    assert.equal(original.filter(event => event.type === 'user/message' && event.data.id === message.id).length, 2);
    await probe.rescan(handle.agent.id);
    await probe.rescan(handle.agent.id);
    assert.deepEqual(probe.evidence.events('synthetic-redelivery'), original);
  } finally { await handle.dispose(); await probe.close(); }
});

test('runtime evidence paths refuse the checkout and relative paths', () => {
  const checkout = fileURLToPath(new URL('../../../', import.meta.url));
  assert.throws(() => assertExternalPath(join(checkout, 'data', 'synthetic.sqlite')), /outside the checkout/);
  assert.throws(() => assertExternalPath('relative-data'), /absolute/);
});
