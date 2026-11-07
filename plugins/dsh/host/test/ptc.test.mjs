import assert from 'node:assert/strict';
import test from 'node:test';
import { load, fixture, hook } from './runtime.mjs';
import { loop, follow, Synthetic } from './loop.mjs';
const { PtcRuntime } = await load('@deepseek-ai/dsh-ptc-runtime');
const { default: LocalFs } = await load('@deepseek-ai/dsh-fs-local');
const { default: WorkingDirectory } = await load('@deepseek-ai/dsh-working-directory');

test('PTC waits for the final dispatch log before delivering a nested value to the program', { timeout: 10000 }, async () => {
  const adapter = new Synthetic(); adapter.names = ['run_code'];
  adapter.arguments = JSON.stringify({ code: 'synthetic runtime fixture', description: 'Synthetic nested call' });
  const host = await loop(adapter);
  const handle = await host.create();
  const saving = Promise.withResolvers();
  const release = Promise.withResolvers();
  let received = false;
  class SyntheticPtc extends PtcRuntime {
    language = 'typescript'; isolation = 'process';
    resolve(request) { return { ...request, cwd: host.root, timeoutMs: 3000 }; }
    async run(spec) {
      const value = await spec.bindings[0].functions.inner({});
      received = true;
      return { logs: [], value };
    }
  }
  try {
    await host.ctx.plugin(LocalFs, { cwd: host.root }).await();
    await host.ctx.plugin(WorkingDirectory, { defaultDirectory: host.root }).await();
    await host.ctx.plugin(SyntheticPtc).await();
    handle.agent.ctx.tools.presentAs('ptc');
    host.ctx.tools.register(fixture('inner'));
    const mutableLog = [{ type: 'text', text: 'synthetic final log preview' }];
    host.ctx.on('tools/ptc-dispatch-log', async () => mutableLog);
    const phases = [];
    const resultErrors = [];
    host.ctx.on(hook, async (exec, result, phase = 'result') => {
      phases.push(phase);
      if (result.isError) resultErrors.push(result.error);
      if (phase === 'ptc-log') {
        assert.equal(result.value.content[0].text, 'synthetic final log preview');
        saving.resolve(); await release.promise;
      }
    });
    follow(handle);
    const idle = handle.agent.whenIdle();
    const first = await Promise.race([saving.promise.then(() => 'saving'), idle.then(() => 'finished')]);
    assert.equal(first, 'saving', JSON.stringify({ phases, received, resultErrors, errors: host.errors.map(error => error.message) }));
    assert.equal(received, false);
    assert.equal(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/ptc-dispatch').length, 0);
    try { mutableLog[0].text = 'synthetic mutation during saving'; } catch (error) { assert.ok(error instanceof TypeError); }
    release.resolve(); await idle;
    assert.equal(received, true);
    assert.equal(handle.agent.session.snapshotEvents().find(e => e.type === 'tool/ptc-dispatch').data.content[0].text, 'synthetic final log preview');
    assert.deepEqual(phases, ['ptc-call', 'result', 'ptc-log', 'result']);
    assert.deepEqual(host.errors, []);
  } finally { release.resolve(); await handle.dispose(); await host.close(); }
});

for (const failure of ['ptc-call', 'result', 'ptc-log']) test(`PTC ${failure} save failure stops the script, later publication and recovery`, { timeout: 10000 }, async () => {
  const adapter = new Synthetic(); adapter.names = ['run_code'];
  adapter.arguments = JSON.stringify({ code: 'synthetic runtime fixture', description: 'Synthetic failed nested call' });
  const host = await loop(adapter);
  const handle = await host.create();
  let effects = 0;
  let received = false;
  class SyntheticPtc extends PtcRuntime {
    language = 'typescript'; isolation = 'process';
    resolve(request) { return { ...request, cwd: host.root, timeoutMs: 3000 }; }
    async run(spec) {
      try { await spec.bindings[0].functions.inner({}); received = true; } catch { /* Script attempts to swallow the failure. */ }
      return { logs: [], value: 'synthetic attempted fallback' };
    }
  }
  const id = handle.agent.session.id;
  let events;
  try {
    await host.ctx.plugin(LocalFs, { cwd: host.root }).await();
    await host.ctx.plugin(WorkingDirectory, { defaultDirectory: host.root }).await();
    await host.ctx.plugin(SyntheticPtc).await();
    handle.agent.ctx.tools.presentAs('ptc');
    host.ctx.tools.register(fixture('inner', async () => { effects++; return [{ type: 'text', text: 'synthetic inner effect' }]; }));
    host.ctx.on(hook, (exec, result, phase = 'result') => {
      if (exec.name === 'inner' && phase === failure) throw new Error('synthetic PTC persistence failure');
    });
    follow(handle); await handle.agent.whenIdle();
    assert.equal(received, false);
    assert.equal(effects, failure === 'ptc-call' ? 0 : 1);
    assert.equal(adapter.requests.length, 1);
    events = handle.agent.session.snapshotEvents();
    assert.equal(events.filter(e => e.type === 'tool/result' || e.type === 'tool/ptc-dispatch').length, 0);
    assert.equal(events.filter(e => e.type === 'tool/ptc-dispatch-start').length, failure === 'ptc-call' ? 0 : 1);
    assert.equal(events.filter(e => e.type === 'plugin:dsh-evernook/publication-blocked').length, 1);
  } finally { await handle.dispose(); await host.close(); }
  const fresh = await loop(new Synthetic(), host.root);
  try {
    assert.deepEqual(await fresh.read(id), events);
    await assert.rejects(fresh.resume(id), /evernook:tool_publication_failed/);
    assert.equal(fresh.adapter.requests.length, 0);
  } finally { await fresh.close(); }
});
