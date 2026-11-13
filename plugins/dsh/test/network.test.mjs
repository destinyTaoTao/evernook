import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('memory links use the confirmed profile page and re-read source on click', async () => {
  let plugin, page, cursor = 0, sourceAvailable = false;
  const state = [], effects = [], refs = [], calls = [];
  const React = {
    Fragment: Symbol('fragment'),
    createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState: initial => {
      const index = cursor++;
      if (!(index in state)) state[index] = initial;
      return [state[index], value => { state[index] = typeof value === 'function' ? value(state[index]) : value; }];
    },
    useRef: initial => {
      const index = cursor++;
      if (!(index in refs)) refs[index] = { current: initial };
      return refs[index];
    },
    useEffect: (effect, deps) => {
      const index = cursor++;
      if (!effects[index] || JSON.stringify(effects[index].deps) !== JSON.stringify(deps)) {
        effects[index]?.cleanup?.();
        effects[index] = { deps, effect };
      }
    },
  };
  const slots = {
    inject: (_name, callback) => callback(),
    register: (options, render) => { if (options.name === 'main') page = render; return () => {}; },
  };
  const ctx = {
    locale: { register: () => () => {}, bind: () => key => key },
    slots, effect: () => {}, inject: () => ({ dispose() {} }), get: () => null,
  };
  const fetch = async (_url, options) => {
    const body = JSON.parse(options.body); calls.push(body);
    if (body.action === 'bootstrap') return Response.json({ csrf: 'test', configured: true, entry: 'dsh' });
    if (body.view === 'profiles') return Response.json({ result: { next_offset: body.offset === 0 ? 24 : null, items: [{
      tag_id: 'tag-1', revision: 2, state: 'confirmed', recorded_at: null,
      draft: { category: 'interest', label: body.offset === 0 ? '合成画像' : '第二页画像', sources: [{ message_id: 'source-1' }] },
    }] } });
    if (body.view === 'source' && !sourceAvailable) return Response.json({ error: 'source_unavailable' }, { status: 400 });
    if (body.view === 'source') return Response.json({ result: { message_id: 'source-1', source: { entry: 'dsh', session: 's' },
      representation: 'original', text: '合成来源', received_at: null, next_cursor: null } });
    return Response.json({ error: 'unexpected' }, { status: 400 });
  };
  const window = { addEventListener() {}, removeEventListener() {},
    __ModuleLoader__: { load: ({ factory }) => { plugin = factory(() => React); } } };
  vm.runInNewContext(readFileSync(new URL('../client.js', import.meta.url), 'utf8'),
    { window, fetch, Response, EventTarget, Event, AbortController, AbortSignal, Date });
  plugin.apply(ctx);
  const render = () => { cursor = 0; const outer = page({ host: 'main' }); return outer.type(outer.props); };
  const flush = async () => {
    for (const entry of effects) if (entry?.effect) { const fn = entry.effect; entry.effect = null; entry.cleanup = fn(); }
    await new Promise(resolve => setTimeout(resolve, 10));
  };
  const find = (node, predicate) => {
    if (!node || typeof node !== 'object') return undefined;
    if (predicate(node)) return node;
    for (const child of node.children ?? []) { const found = find(child, predicate); if (found) return found; }
  };
  let tree = render(); await flush(); tree = render();
  const network = find(tree, node => node.type === 'button' && node.children.includes('network'));
  assert.ok(network); network.props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.props?.className === 'en-network-cluster'));
  assert.ok(find(tree, node => node.children?.includes('合成画像')));
  assert.ok(calls.some(call => call.action === 'read' && call.view === 'profiles'));
  const source = find(tree, node => node.type === 'button' && node.children.includes('networkSource 1'));
  assert.ok(source); source.props.onClick(); await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls.some(call => call.action === 'read' && call.view === 'source' && call.message_id === 'source-1'));
  tree = render();
  assert.ok(find(tree, node => node.props?.role === 'alert' && node.children.includes('sourceUnavailable')));
  assert.equal(find(tree, node => node.children?.includes('合成来源')), undefined);
  sourceAvailable = true;
  source.props.onClick(); await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  const modal = find(tree, node => node.props?.title === 'raw');
  assert.ok(modal);
  assert.ok(find(modal, node => node.children?.includes('合成来源')));
  modal.props.close(); tree = render();
  const next = find(tree, node => node.type === 'button' && node.children.includes('next'));
  assert.ok(next); assert.equal(next.props.disabled, false); next.props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(calls.some(call => call.view === 'profiles' && call.offset === 24));
  assert.ok(find(tree, node => node.children?.includes('第二页画像')));
});
