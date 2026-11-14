import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('memory links use the confirmed profile page and re-read source on click', async () => {
  let plugin, page, cursor = 0, sourceAvailable = false, statusAvailable = true, statusModelsAvailable = true, timelineAvailable = true, delaySource = false, delayGraph = false, resolveSource, resolveHistory, resolveGraph;
  const state = [], effects = [], refs = [], calls = [];
  const windowListeners = new Map();
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
    const respond = value => Response.json({ result: body.action === 'read-versioned' ? { value, token: 'a'.repeat(64) } : value });
    if (body.action === 'bootstrap') return Response.json({ csrf: 'test', configured: true, entry: 'dsh', private_capture: true, fingerprint_bridge: true });
    if (body.action === 'private_open') return Response.json({ result: { requested: true } });
    if (body.action === 'private_status') return Response.json({ result: { state: 'outcome_unknown',
      session_id: '12345678-1234-4123-8123-123456789abc', event_id: '87654321-4321-4321-8321-cba987654321' } });
    if (body.action === 'private_retry') return Response.json({ result: { requested: true } });
    if (body.action === 'fingerprints') return Response.json({ result: { tokens: body.requests.map(() => 'a'.repeat(64)) } });
    if (body.view === 'status') return statusAvailable
      ? Response.json({ result: { configuration_schema_version: 1, daily_admitted: false,
        ...(statusModelsAvailable ? { model_configuration: { embedding: 'configured_local', reflection: 'configured_remote', speech: 'host_owned' } } : {}) } })
      : Response.json({ error: 'management_unavailable' }, { status: 400 });
    if (body.view === 'profiles' || body.view === 'network_profiles') return respond({ next_offset: body.offset === 0 ? 24 : null, items: [{
      tag_id: 'tag-1', revision: 2, state: 'confirmed', recorded_at: null,
      draft: { category: 'interest', label: body.offset === 0 ? '合成画像' : '第二页画像', sources: [{ message_id: 'source-1' }] },
    }] });
    if (body.view === 'network_facts') return respond({ next_offset: null, items: [{
      fact_id: 'fact-1', revision: 3, state: 'disputed', text: '合成事实', sources: ['source-1'],
    }] });
    if (body.view === 'network_observations') return respond({ next_offset: null, items: [{
      observation_id: 'obs-1', revision: 2, state: 'published', inference: true,
      conclusion: '合成观察', support: ['source-1'], counterevidence: ['source-2'],
    }] });
    if (body.view === 'network_graph' && delayGraph) return new Promise(resolve => { resolveGraph = resolve; });
    if (body.view === 'network_graph') return respond({ next_offset: null, nodes: [
      { kind: 'profile', id: 'p', revision: 2, state: 'confirmed', label: '合成画像', edges: [{ source_id: 'source-1', kind: 'evidence' }] },
      { kind: 'fact', id: 'f', revision: 1, state: 'current', label: '合成事实', edges: [{ source_id: 'source-1', kind: 'evidence' }] },
      { kind: 'observation', id: 'o', revision: 1, state: 'conflicted', inference: true, label: '合成观察', edges: [{ source_id: 'source-1', kind: 'counterevidence' }] },
      { kind: 'topic', id: 't', revision: 1, state: 'valid', inference: true, label: '合成主题', edges: [{ source_id: 'source-1', kind: 'evidence' }] },
    ] });
    if (body.view === 'timeline') return timelineAvailable ? respond({ next_offset: null, items: [] })
      : Response.json({ error: 'management_unavailable' }, { status: 400 });
    if (body.view === 'history') return new Promise(resolve => { resolveHistory = resolve; });
    if (body.view === 'source' && !sourceAvailable) return Response.json({ error: 'source_unavailable' }, { status: 400 });
    if (body.view === 'source' && delaySource) return new Promise(resolve => { resolveSource = resolve; });
    if (body.view === 'source') return respond({ message_id: 'source-1', source: { entry: 'dsh', session: 's' },
      representation: 'original', text: '合成来源', received_at: null, next_cursor: null });
    return Response.json({ error: 'unexpected' }, { status: 400 });
  };
  const window = { addEventListener(name, callback) { windowListeners.set(name, callback); }, removeEventListener(name) { windowListeners.delete(name); },
    __ModuleLoader__: { load: ({ factory }) => { plugin = factory(() => React); } } };
  vm.runInNewContext(readFileSync(new URL('../client.js', import.meta.url), 'utf8'),
    { window, fetch, Response, EventTarget, Event, AbortController, AbortSignal, Date, performance, setTimeout: () => 1, clearTimeout() {} });
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
  find(tree, node => node.type === 'button' && node.children.includes('privateOpen')).props.onClick();
  await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  assert.ok(calls.some(call => JSON.stringify(call) === JSON.stringify({ action: 'private_open' })));
  assert.ok(find(tree, node => node.children?.includes('privateRequested')));
  find(tree, node => node.type === 'button' && node.children.includes('privateCheck')).props.onClick();
  await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  assert.ok(find(tree, node => node.children?.includes('privateUnknown')));
  find(tree, node => node.type === 'button' && node.children.includes('privateRetry')).props.onClick();
  await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  assert.ok(calls.some(call => JSON.stringify(call) === JSON.stringify({ action: 'private_retry' })));
  assert.ok(find(tree, node => node.children?.includes('diagnostics')));
  assert.ok(find(tree, node => node.children?.includes('diagnosticPrivate: diagnosticConfigured')));
  assert.ok(find(tree, node => node.children?.includes('diagnosticFingerprint: diagnosticConfigured')));
  assert.ok(find(tree, node => node.children?.includes('diagnosticEmbedding: providerLocal')));
  assert.ok(find(tree, node => node.children?.includes('diagnosticReflection: providerRemote')));
  assert.ok(find(tree, node => node.children?.includes('diagnosticSpeech: providerHost')));
  assert.ok(calls.some(call => call.view === 'status'));
  statusModelsAvailable = false;
  find(tree, node => node.type === 'button' && node.children.includes('refresh')).props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.children?.includes('diagnosticEmbedding: providerUnknown')));
  statusModelsAvailable = true;
  statusAvailable = false;
  find(tree, node => node.type === 'button' && node.children.includes('refresh')).props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.children?.some(child => typeof child === 'string' && child.includes('diagnosticUnavailable'))));
  assert.ok(find(tree, node => node.children?.includes('diagnosticEmbedding: providerUnknown')));
  statusAvailable = true;
  find(tree, node => node.type === 'button' && node.children.includes('history')).props.onClick();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(resolveHistory);
  const network = find(tree, node => node.type === 'button' && node.children.includes('network'));
  assert.ok(network); network.props.onClick();
  tree = render(); await flush(); tree = render();
  resolveHistory(Response.json({ result: { value: { items: [], next_offset: null }, token: 'a'.repeat(64) } }));
  await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  assert.equal(find(tree, node => node.props?.title === 'history'), undefined);
  assert.ok(find(tree, node => node.props?.className === 'en-network-cluster'));
  assert.ok(find(tree, node => node.children?.includes('合成事实')));
  assert.ok(find(tree, node => node.children?.includes('合成观察')));
  assert.ok(find(tree, node => node.children?.includes('合成主题')));
  assert.ok(calls.some(call => call.view === 'network_graph'));
  delayGraph = true; windowListeners.get('focus')(); tree = render(); await flush();
  assert.ok(resolveGraph);
  delayGraph = false; windowListeners.get('focus')(); tree = render(); await flush();
  resolveGraph(Response.json({ result: { value: { next_offset: null, nodes: [{ kind: 'fact', id: 'old',
    revision: 1, state: 'current', label: '过期图', edges: [{ source_id: 'source-1', kind: 'evidence' }] }] }, token: 'a'.repeat(64) } }));
  await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  assert.equal(find(tree, node => node.children?.includes('过期图')), undefined);
  assert.ok(find(tree, node => node.children?.includes('合成事实')));
  const graphLayer = find(tree, node => node.type === 'select' && node.props?.['aria-label'] === 'networkLayer');
  graphLayer.props.onChange({ target: { value: 'profiles' } });
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.props?.className === 'en-network-cluster'));
  assert.ok(find(tree, node => node.children?.includes('合成画像')));
  assert.ok(calls.some(call => call.action === 'read-versioned' && call.view === 'network_profiles'));
  const source = find(tree, node => node.type === 'button' && node.children.includes('networkSource 1'));
  assert.ok(source); source.props.onClick(); await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls.some(call => call.action === 'read-versioned' && call.view === 'source' && call.message_id === 'source-1'));
  tree = render();
  assert.ok(find(tree, node => node.props?.role === 'alert' && node.children.includes('sourceUnavailable')));
  assert.equal(find(tree, node => node.children?.includes('合成来源')), undefined);
  sourceAvailable = true;
  source.props.onClick(); await new Promise(resolve => setTimeout(resolve, 10)); tree = render();
  const modal = find(tree, node => node.props?.title === 'raw');
  assert.ok(modal);
  assert.ok(find(modal, node => node.children?.includes('合成来源')));
  modal.props.close(); tree = render();
  delaySource = true;
  source.props.onClick();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(resolveSource);
  windowListeners.get('focus')();
  tree = render(); await flush();
  resolveSource(Response.json({ result: { value: { message_id: 'source-1', source: { entry: 'dsh', session: 's' },
    representation: 'original', text: '过期来源', received_at: null, next_cursor: null }, token: 'a'.repeat(64) } }));
  await new Promise(resolve => setTimeout(resolve, 10));
  tree = render();
  assert.equal(find(tree, node => node.props?.title === 'raw'), undefined);
  delaySource = false;
  const next = find(tree, node => node.type === 'button' && node.children.includes('next'));
  assert.ok(next); assert.equal(next.props.disabled, false); next.props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(calls.some(call => call.view === 'network_profiles' && call.offset === 24));
  assert.ok(find(tree, node => node.children?.includes('第二页画像')));
  const layer = find(tree, node => node.type === 'select' && node.props?.['aria-label'] === 'networkLayer');
  assert.ok(layer); layer.props.onChange({ target: { value: 'facts' } });
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.children?.includes('合成事实')));
  assert.ok(find(tree, node => node.children?.includes('disputed')));
  assert.ok(calls.some(call => call.view === 'network_facts'));
  find(tree, node => node.type === 'input' && node.props['aria-label'] === 'filterEntry').props.onChange({ target: { value: 'dsh' } });
  find(tree, node => node.type === 'input' && node.props['aria-label'] === 'filterSession').props.onChange({ target: { value: 'evidence-session' } });
  tree = render(); find(tree, node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  tree = render(); await flush(); tree = render();
  assert.ok(calls.some(call => call.view === 'network_facts' && call.entry === 'dsh' && call.session_id === 'evidence-session'));
  layer.props.onChange({ target: { value: 'observations' } });
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.children?.includes('合成观察')));
  assert.ok(find(tree, node => node.children?.includes('inference')));
  const counter = find(tree, node => node.type === 'button' && node.children?.includes('counterevidence 1'));
  assert.ok(counter); counter.props.onClick(); await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls.some(call => call.view === 'source' && call.message_id === 'source-2'));
  assert.ok(calls.some(call => call.view === 'network_observations'));
  find(tree, node => node.type === 'button' && node.children.includes('timeline')).props.onClick();
  tree = render(); await flush(); tree = render();
  find(tree, node => node.type === 'input' && node.props['aria-label'] === 'filterEntry').props.onChange({ target: { value: 'dsh' } });
  find(tree, node => node.type === 'input' && node.props['aria-label'] === 'filterSession').props.onChange({ target: { value: 's1' } });
  find(tree, node => node.type === 'input' && node.props['aria-label'] === 'filterSince').props.onChange({ target: { value: '2022-01-01' } });
  tree = render(); find(tree, node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  tree = render(); await flush();
  assert.ok(calls.some(call => call.view === 'timeline' && call.entry === 'dsh' && call.session_id === 's1'
    && call.since === new Date('2022-01-01T00:00:00').toISOString()));
  tree = render();
  assert.ok(find(tree, node => node.type === 'h2' && node.children.includes('empty')));
  timelineAvailable = false;
  find(tree, node => node.type === 'button' && node.children.includes('refresh')).props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.props?.role === 'alert'));
  assert.equal(find(tree, node => node.type === 'h2' && node.children.includes('empty')), undefined);
  assert.equal(find(tree, node => node.props?.className === 'en-page'), undefined);
  timelineAvailable = true;
  find(tree, node => node.type === 'button' && node.children.includes('refresh')).props.onClick();
  tree = render(); await flush(); tree = render();
  assert.ok(find(tree, node => node.type === 'h2' && node.children.includes('empty')));
  assert.ok(find(tree, node => node.props?.className === 'en-page'));
  cursor = 0; const scoped = page({ host: 'sidebar', sessionId: 'sidebar-session' }); scoped.type(scoped.props); await flush();
  assert.ok(calls.some(call => call.view === 'timeline' && call.session_id === 'sidebar-session'));
  const before = calls.length;
  cursor = 0; const sidebar = page({ host: 'sidebar' }); sidebar.type(sidebar.props); await flush();
  assert.equal(calls.slice(before).some(call => call.view === 'timeline'), false);
});
