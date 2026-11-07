import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

test('browser plugin owns slot injections and its optional sidebar fiber', async () => {
  let plugin: { apply(ctx: unknown): void } | undefined;
  let slots = 0, sidebar = 0, fiberDisposed = false;
  const cleanups: (() => unknown)[] = [];
  const childCleanups: (() => unknown)[] = [];
  const slotService = {
    inject: (_name: string, callback: () => () => void) => callback(),
    register: () => { slots++; let active = true; return () => { if (active) { slots--; active = false; } }; },
  };
  const ctx = {
    locale: { register: () => () => {}, bind: () => (key: string) => key },
    slots: slotService,
    effect: (callback: () => () => unknown) => { cleanups.push(callback()); },
    inject: (_keys: string[], callback: (child: unknown) => void) => {
      callback({ slots: slotService, sidebarRightTabs: { register: () => { sidebar++; return () => { sidebar--; }; } },
        effect: (effect: () => () => unknown) => { childCleanups.push(effect()); } });
      return { dispose: async () => { fiberDisposed = true; for (const cleanup of childCleanups) await cleanup(); } };
    },
  };
  vm.runInNewContext(readFileSync(new URL('../../dsh/client.js', import.meta.url), 'utf8'), {
    EventTarget, Event, AbortSignal,
    window: { __ModuleLoader__: { load: ({ factory }: { factory(require: (name: string) => unknown): typeof plugin }) => {
      plugin = factory(() => ({ createElement: () => null }));
    } } },
  });
  assert.ok(plugin);
  plugin.apply(ctx);
  assert.equal(slots, 4);
  assert.equal(sidebar, 1);
  for (const cleanup of cleanups.reverse()) await cleanup();
  assert.equal(slots, 0);
  assert.equal(sidebar, 0);
  assert.equal(fiberDisposed, true);
});
