import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('visible projections clear on change, timeout and background; late responses stay hidden', async () => {
  let plugin, page, cursor = 0, now = 0, nextTimer = 0, version = 'a', sourceVersion = 'c', historyVersion = 'e', hang = false, forceMismatch = false, resolveLate;
  const state = [], refs = [], effects = [], timers = new Map(), listeners = new Map();
  const React = {
    Fragment: Symbol('fragment'), createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState: initial => { const i = cursor++; if (!(i in state)) state[i] = initial;
      return [state[i], value => { state[i] = typeof value === 'function' ? value(state[i]) : value; }]; },
    useRef: initial => { const i = cursor++; if (!(i in refs)) refs[i] = { current: initial }; return refs[i]; },
    useEffect: (effect, deps) => { const i = cursor++; if (!effects[i] || JSON.stringify(effects[i].deps) !== JSON.stringify(deps)) {
      effects[i]?.cleanup?.(); effects[i] = { effect, deps };
    } },
  };
  const document = { visibilityState: 'visible', addEventListener: (n, f) => listeners.set(n, f), removeEventListener: n => listeners.delete(n) };
  const window = { document, addEventListener: (n, f) => listeners.set(n, f), removeEventListener: n => listeners.delete(n),
    __ModuleLoader__: { load: ({ factory }) => { plugin = factory(() => React); } } };
  const fakeTimeout = (fn, delay) => { const id = ++nextTimer; timers.set(id, { at: now + delay, fn }); return id; };
  const fakeClear = id => timers.delete(id);
  class FakeDate extends Date { static now() { return now; } }
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const advance = async amount => { const target = now + amount;
    while (true) { const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break; now = due[1].at; timers.delete(due[0]); due[1].fn(); await settle(); }
    now = target; await settle();
  };
  const fetch = async (_url, options) => {
    const request = JSON.parse(options.body);
    if (request.action === 'bootstrap') return Response.json({ csrf: 'test', configured: true, entry: 'dsh' });
    if (request.view === 'status') return Response.json({ result: { configuration_schema_version: 1 } });
    if (request.action === 'fingerprint') return hang ? new Promise(resolve => { resolveLate = resolve; }) : Response.json({ result: { token: (forceMismatch ? 'f' : request.view === 'source' ? sourceVersion : request.view === 'history' ? historyVersion : version).repeat(64) } });
    if (request.action === 'read-versioned' && request.view === 'source') return Response.json({ result: { token: sourceVersion.repeat(64), value: {
      message_id: 's', state_version: 1, content_version: request.cursor ? 2 : 1, source: { entry: 'test', session: 's' }, representation: 'original', text: '合成原文', next_cursor: request.cursor ? null : 4 } } });
    if (request.action === 'read-versioned' && request.view === 'history') return Response.json({ result: { token: historyVersion.repeat(64), value: { items: [], next_offset: null } } });
    if (request.action === 'read-versioned') return Response.json({ result: { token: version.repeat(64), value: { next_offset: null,
      items: [{ tag_id: 'p', revision: 1, state: 'confirmed', draft: { category: 'interest', label: '合成画像' + version, sources: [{ message_id: 's' }] } }] } } });
    throw new Error('unexpected request');
  };
  vm.runInNewContext(readFileSync(new URL('../client.js', import.meta.url), 'utf8'),
    { window, fetch, Response, EventTarget, Event, AbortController, AbortSignal, Date: FakeDate, performance: { now: () => now }, setTimeout: fakeTimeout, clearTimeout: fakeClear });
  const ctx = { locale: { register: () => () => {}, bind: () => key => key },
    slots: { inject: (_name, callback) => callback(), register: (options, render) => { if (options.name === 'main') page = render; return () => {}; } },
    effect: () => {}, inject: () => ({ dispose() {} }), get: () => null };
  plugin.apply(ctx);
  const render = () => { cursor = 0; const outer = page({ host: 'main' }); return outer.type(outer.props); };
  const flush = async () => { for (const entry of effects) if (entry?.effect) { const fn = entry.effect; entry.effect = null; entry.cleanup = fn(); } await settle(); };
  const contains = (node, value) => node && typeof node === 'object' && (node.children?.includes(value) || (node.children ?? []).some(child => contains(child, value)));
  const find = (node, predicate) => { if (!node || typeof node !== 'object') return undefined; if (predicate(node)) return node;
    for (const child of node.children ?? []) { const match = find(child, predicate); if (match) return match; } };
  let tree = render(); await flush(); tree = render();
  assert.ok(contains(tree, '合成画像a'));
  await advance(3000); tree = render(); assert.ok(contains(tree, '合成画像a'));
  find(tree, node => node.type === 'button' && node.children.includes('source 1')).props.onClick(); await settle(); tree = render();
  assert.ok(find(tree, node => node.props?.title === 'raw'));
  find(tree, node => node.type === 'button' && node.children.includes('more')).props.onClick(); await settle(); tree = render();
  assert.equal(find(tree, node => node.props?.title === 'raw'), undefined);
  listeners.get('focus')(); tree = render(); await flush(); tree = render();
  find(tree, node => node.type === 'button' && node.children.includes('source 1')).props.onClick(); await settle(); tree = render();
  assert.ok(find(tree, node => node.props?.title === 'raw'));
  sourceVersion = 'd'; await advance(3000); tree = render(); assert.equal(find(tree, node => node.props?.title === 'raw'), undefined);
  await flush(); tree = render(); assert.ok(contains(tree, '合成画像a'));
  find(tree, node => node.type === 'button' && node.children.includes('history')).props.onClick(); await settle(); tree = render();
  assert.ok(find(tree, node => node.props?.title === 'history'));
  historyVersion = 'f'; await advance(3000); tree = render(); assert.equal(find(tree, node => node.props?.title === 'history'), undefined);
  await flush(); tree = render(); assert.ok(contains(tree, '合成画像a'));
  version = 'b'; await advance(3000); tree = render(); assert.ok(!contains(tree, '合成画像a'));
  await flush(); tree = render(); assert.ok(contains(tree, '合成画像b'));
  hang = true; await advance(3000); await advance(12000); tree = render(); assert.ok(!contains(tree, '合成画像b'));
  resolveLate(Response.json({ result: { token: version.repeat(64) } })); await settle(); tree = render(); assert.ok(!contains(tree, '合成画像b'));
  hang = false; listeners.get('focus')(); tree = render(); await flush(); tree = render(); assert.ok(contains(tree, '合成画像b'));
  document.visibilityState = 'hidden'; listeners.get('visibilitychange')(); tree = render(); assert.ok(!contains(tree, '合成画像b'));
  document.visibilityState = 'visible'; listeners.get('visibilitychange')(); tree = render(); await flush(); tree = render(); assert.ok(contains(tree, '合成画像b'));
  forceMismatch = true;
  for (let attempt = 0; attempt < 4; attempt += 1) { await advance(3000); tree = render(); if (attempt < 3) { await flush(); tree = render(); } }
  assert.ok(!contains(tree, '合成画像b'));
  assert.ok(contains(tree, 'unavailable'));
  assert.equal(timers.size, 0);
});
