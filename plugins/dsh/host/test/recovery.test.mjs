import assert from 'node:assert/strict';
import test from 'node:test';
import { loop } from './loop.mjs';
import { call, fixture, load } from './runtime.mjs';
const { appendPluginRecord, buildForkSeed, SessionId, SessionSeq, SessionLogOffset } = await load('@deepseek-ai/dsh-session');

test('a durable recovery marker denies new tool identities without executing tool hooks or finalizers', async () => {
  const host = await loop();
  const handle = await host.create();
  let effects = 0; let policies = 0; let finalizers = 0;
  try {
    appendPluginRecord(handle.agent.session, 'plugin:dsh-evernook/recovery-readonly', { version: 1 });
    await host.ctx.sessions.flush(handle.agent.session);
    host.ctx.tools.register({ ...fixture('synthetic-tool', async () => { effects++; return [{ type: 'text', text: 'must not run' }]; }),
      finalizeContent: () => { finalizers++; return [{ type: 'text', text: 'must not finalize' }]; } });
    host.ctx.on('tools/pre-execute', (exec, next) => { policies++; return next(); });
    for (const callId of ['new-identity-1', 'new-identity-2']) {
      const result = await call(host.ctx, 'synthetic-tool', { agent: handle.agent, callId });
      assert.equal(result.isError, true);
      assert.equal(result.error.info.code, 'EVERNOOK_RECOVERY_READ_ONLY');
    }
    assert.deepEqual({ effects, policies, finalizers }, { effects: 0, policies: 0, finalizers: 0 });
  } finally { await handle.dispose(); await host.close(); }
});

test('read-only recovery survives restart and forks even before the explicit policy record', async () => {
  let host = await loop();
  let handle = await host.create();
  const id = handle.agent.session.id; const root = host.root;
  handle.agent.session.append('session/end-seed', {});
  appendPluginRecord(handle.agent.session, 'plugin:dsh-evernook/recovery-readonly', { version: 1 });
  await host.ctx.sessions.flush(handle.agent.session);
  await handle.dispose(); await host.close();
  host = await loop(undefined, root);
  handle = await host.resume(id);
  let effects = 0;
  const children = [];
  try {
    host.ctx.tools.register(fixture('synthetic-tool', async () => { effects++; return [{ type: 'text', text: 'must not run' }]; }));
    let parent = handle.agent.session;
    for (let generation = 0; generation < 2; generation++) {
      const seed = buildForkSeed(parent.snapshotEvents(), SessionSeq(0));
      const child = await host.ctx.agents.create({ sessionId: SessionId('synthetic-recovery-fork-' + generation), seed,
        inheritedEventCount: SessionLogOffset(1), meta: { cwd: root, parentSession: parent.id, isSeeded: true },
        agentOptions: { provider: 'synthetic', model: 'local-test' } });
      children.push(child);
      const result = await call(host.ctx, 'synthetic-tool', { agent: child.agent, callId: 'new-fork-call-' + generation });
      assert.equal(result.isError, true);
      assert.equal(result.error.info.code, 'EVERNOOK_RECOVERY_READ_ONLY');
      assert.equal(effects, 0);
      parent = child.agent.session;
    }
  } finally { for (const child of children.reverse()) await child.dispose(); await handle.dispose(); await host.close(); }
});

test('a reserved recovery identity fails closed before its policy record is appended', async () => {
  const host = await loop();
  const handle = await host.ctx.agents.create({ sessionId: SessionId('evernook-recovery-synthetic-incomplete'),
    meta: { cwd: host.root }, agentOptions: { provider: 'synthetic', model: 'local-test' } });
  let effects = 0;
  try {
    host.ctx.tools.register(fixture('synthetic-tool', async () => { effects++; return []; }));
    const result = await call(host.ctx, 'synthetic-tool', { agent: handle.agent, callId: 'new-incomplete-call' });
    assert.equal(result.error.info.code, 'EVERNOOK_RECOVERY_READ_ONLY');
    assert.equal(effects, 0);
  } finally { await handle.dispose(); await host.close(); }
});
