import assert from 'node:assert/strict';
import test from 'node:test';
import { fixture, hook } from './runtime.mjs';
import { loop, follow, Synthetic } from './loop.mjs';

test('agent loop stops on save failure without manufacturing a tool result', { timeout: 10000 }, async () => {
  const host = await loop();
  const handle = await host.create();
  try {
    let effects = 0;
    let saves = 0;
    host.ctx.tools.register(fixture('synthetic-tool', async () => { effects++; return [{ type: 'text', text: 'synthetic side effect' }]; }));
    host.ctx.on(hook, () => { saves++; throw new Error('synthetic storage unavailable'); });
    follow(handle);
    await handle.agent.whenIdle();
    const events = handle.agent.session.snapshotEvents();
    assert.equal(events.filter(e => e.type === 'tool/result').length, 0);
    assert.equal(events.filter(e => e.type === 'tool/call').length, 1);
    assert.equal(host.adapter.requests.length, 1);
    assert.deepEqual({ effects, saves }, { effects: 1, saves: 1 });
    assert.equal(events.findLast(e => e.type === 'plugin:dsh-evernook/publication-blocked').data.code, 'EVERNOOK_TOOL_PUBLICATION_FAILED');
    assert.equal(events.some(e => e.type === 'step/end' || e.type === 'turn/end'), false);
    follow(handle);
    await handle.agent.whenIdle();
    assert.equal(host.adapter.requests.length, 1);
    assert.deepEqual({ effects, saves }, { effects: 1, saves: 1 });
  } finally { await handle.dispose(); await host.close(); }
});

test('parallel results save in model order despite reverse body completion', { timeout: 10000 }, async () => {
  const adapter = new Synthetic(); adapter.names = ['first', 'second'];
  const host = await loop(adapter);
  const handle = await host.create();
  const secondFinished = Promise.withResolvers();
  const saving = Promise.withResolvers();
  const release = Promise.withResolvers();
  try {
    const bodies = [];
    const saves = [];
    for (const name of adapter.names) host.ctx.tools.register({ ...fixture(name, async () => {
      if (name === 'first') await secondFinished.promise;
      bodies.push(name);
      if (name === 'second') secondFinished.resolve();
      return [{ type: 'text', text: name + '🙂\r\n37.50元。' }];
    }), isConcurrencySafe: () => true });
    host.ctx.on(hook, async (exec, result) => {
      saves.push(exec.name);
      if (exec.name === 'first') { saving.resolve(); await release.promise; }
      assert.equal(result.content[0].text, exec.name + '🙂\r\n37.50元。');
    });
    follow(handle);
    const idle = handle.agent.whenIdle();
    await saving.promise;
    assert.deepEqual(bodies, ['second', 'first']);
    assert.deepEqual(saves, ['first']);
    assert.equal(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/result').length, 0);
    release.resolve(); await idle;
    assert.deepEqual(saves, ['first', 'second']);
    assert.deepEqual(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/result').map(e => e.data.message.content[0].text), ['first🙂\r\n37.50元。', 'second🙂\r\n37.50元。']);
    assert.equal(adapter.requests.length, 2);
    assert.deepEqual(host.errors, []);
  } finally { secondFinished.resolve(); release.resolve(); await handle.dispose(); await host.close(); }
});

test('saving failure during cancellation blocks skipped results and persists its failure category', { timeout: 10000 }, async () => {
  const adapter = new Synthetic(); adapter.names = ['first', 'skipped'];
  const host = await loop(adapter);
  const handle = await host.create();
  try {
    let effects = 0;
    const saves = [];
    host.ctx.tools.register(fixture('first', async () => { effects++; handle.agent.cancel({ kind: 'user' }); return [{ type: 'text', text: 'first' }]; }));
    host.ctx.tools.register(fixture('skipped', async () => { effects++; return [{ type: 'text', text: 'must not run' }]; }));
    host.ctx.on(hook, exec => { saves.push(exec.name); if (exec.name === 'skipped') throw new Error('synthetic canceled result save failure'); });
    follow(handle); await handle.agent.whenIdle();
    assert.equal(effects, 1);
    assert.deepEqual(saves, ['first', 'skipped']);
    const events = handle.agent.session.snapshotEvents();
    assert.equal(events.filter(e => e.type === 'tool/call').length, 2);
    assert.equal(events.filter(e => e.type === 'tool/result').length, 1);
    assert.equal(events.findLast(e => e.type === 'plugin:dsh-evernook/publication-blocked').data.code, 'EVERNOOK_TOOL_PUBLICATION_FAILED');
    assert.equal(adapter.requests.length, 1);
  } finally { await handle.dispose(); await host.close(); }
});

test('fresh host refuses a failed session without manufacturing recovery results or calling the provider', { timeout: 10000 }, async () => {
  const host = await loop();
  const handle = await host.create();
  const id = handle.agent.session.id;
  let events;
  try {
    host.ctx.tools.register(fixture());
    host.ctx.on(hook, () => { throw new Error('synthetic failed receipt'); });
    follow(handle); await handle.agent.whenIdle();
    await host.ctx.sessions.flush(handle.agent.session);
    events = handle.agent.session.snapshotEvents();
  } finally { await handle.dispose(); await host.close(); }
  const fresh = await loop(new Synthetic(), host.root);
  try {
    await assert.rejects(fresh.resume(id), /evernook:tool_publication_failed/);
    assert.equal(fresh.adapter.requests.length, 0);
    assert.deepEqual(await fresh.read(id), events);
  } finally { await fresh.close(); }
});

test('failed first parallel save drains side effects but publishes no later result', { timeout: 10000 }, async () => {
  const adapter = new Synthetic(); adapter.names = ['first', 'second'];
  const host = await loop(adapter);
  const handle = await host.create();
  try {
    let effects = 0;
    let saves = 0;
    for (const name of adapter.names) host.ctx.tools.register({ ...fixture(name, async () => {
      effects++; return [{ type: 'text', text: name }];
    }), isConcurrencySafe: () => true });
    host.ctx.on(hook, () => { saves++; throw new Error('synthetic parallel save failure'); });
    follow(handle); await handle.agent.whenIdle();
    assert.equal(effects, 2);
    assert.equal(saves, 1);
    assert.equal(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/result').length, 0);
    assert.equal(adapter.requests.length, 1);
  } finally { await handle.dispose(); await host.close(); }
});

test('scheduler awaits even the final-error branch before appending its session result', { timeout: 10000 }, async () => {
  const host = await loop();
  const handle = await host.create();
  const saving = Promise.withResolvers();
  const release = Promise.withResolvers();
  try {
    host.ctx.tools.register(fixture());
    host.ctx.on('tools/pre-execute', () => { throw new Error('synthetic pre-policy failure'); });
    host.ctx.on(hook, async () => { saving.resolve(); await release.promise; });
    follow(handle);
    const idle = handle.agent.whenIdle();
    await saving.promise;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/result').length, 0);
    release.resolve();
    await idle;
    assert.equal(handle.agent.session.snapshotEvents().filter(e => e.type === 'tool/result').length, 1);
    assert.deepEqual(host.errors, []);
  } finally { release.resolve(); await handle.dispose(); await host.close(); }
});
