import assert from 'node:assert/strict';
import test from 'node:test';
import { service, fixture, call, hook } from './runtime.mjs';

test('final result and notification wait until saving finishes', { timeout: 5000 }, async () => {
  const ctx = await service();
  const saving = Promise.withResolvers();
  const saved = Promise.withResolvers();
  const notices = [];
  try {
    ctx.tools.register(fixture());
    ctx.on('tools/result', (exec, result) => { notices.push(result); });
    let snapshot;
    ctx.on(hook, async (exec, result) => {
      snapshot = result;
      assert.equal(Object.isFrozen(exec), true);
      assert.equal(Object.isFrozen(result), true);
      assert.equal(Object.isFrozen(result.content), true);
      saving.resolve();
      await saved.promise;
    });
    const executing = call(ctx);
    const first = await Promise.race([saving.promise.then(() => 'saving'), executing.then(() => 'published')]);
    assert.equal(first, 'saving');
    assert.equal(notices.length, 0);
    saved.resolve();
    const result = await executing;
    assert.equal(result, snapshot);
    assert.deepEqual(notices, [snapshot]);
    assert.equal(result.content[0].text, '合成最终结果🙂\r\n37.50元。');
  } finally { saved.resolve(); await ctx.fiber.dispose(); }
});

test('removing a save hook during a started tool cannot bypass its original barrier', { timeout: 5000 }, async () => {
  const ctx = await service();
  const started = Promise.withResolvers();
  const releaseBody = Promise.withResolvers();
  try {
    ctx.tools.register(fixture('synthetic-tool', async () => {
      started.resolve(); await releaseBody.promise; return [{ type: 'text', text: 'synthetic started effect' }];
    }));
    let saves = 0;
    const remove = ctx.on(hook, () => { saves++; throw new Error('synthetic adapter disposed'); });
    const executing = call(ctx);
    const failed = assert.rejects(executing, error => error.code === 'EVERNOOK_TOOL_PUBLICATION_FAILED');
    await started.promise; remove(); releaseBody.resolve();
    await failed;
    assert.equal(saves, 1);
  } finally { releaseBody.resolve(); await ctx.fiber.dispose(); }
});

test('a nested save failure cannot be swallowed by an outer tool', { timeout: 5000 }, async () => {
  const ctx = await service();
  try {
    let effects = 0;
    let notices = 0;
    let saves = 0;
    ctx.tools.register(fixture('inner', async () => { effects++; return [{ type: 'text', text: 'inner side effect' }]; }));
    ctx.tools.register(fixture('outer', async (args, exec) => {
      try { await call(ctx, 'inner', { parent: exec.token, rootCallId: exec.rootCallId }); } catch { /* Deliberately hostile wrapper. */ }
      try { await call(ctx, 'inner', { callId: 'another-effect' }); } catch { /* Must not execute twice. */ }
      return [{ type: 'text', text: 'outer attempted to hide the failure' }];
    }));
    ctx.on(hook, exec => { saves++; assert.equal(exec.name, 'inner'); throw new Error('synthetic nested save failure'); });
    ctx.on('tools/result', () => { notices++; });
    await assert.rejects(call(ctx, 'outer'), error => error.code === 'EVERNOOK_TOOL_PUBLICATION_FAILED');
    assert.deepEqual({ effects, saves, notices }, { effects: 1, saves: 1, notices: 0 });
  } finally { await ctx.fiber.dispose(); }
});

test('final transformation and every error/cancellation branch cross the same barrier', { timeout: 5000 }, async () => {
  for (const branch of ['transformed', 'body-error', 'post-block', 'pre-deny', 'pre-throw', 'already-canceled', 'canceled-in-body', 'unknown', 'invalid-args']) {
    const ctx = await service();
    const controller = new AbortController();
    try {
      let effects = 0;
      const definition = fixture('synthetic-tool', async () => {
        effects++;
        if (branch === 'body-error') throw new Error('synthetic body error');
        if (branch === 'canceled-in-body') controller.abort();
        return [{ type: 'text', text: 'before final transform' }];
      });
      ctx.tools.register({ ...definition, finalizeContent: () => [{ type: 'text', text: 'after final transform🙂' }] });
      if (branch === 'pre-deny') ctx.on('tools/pre-execute', () => ({ kind: 'deny', reason: 'synthetic permission denied' }));
      if (branch === 'pre-throw') ctx.on('tools/pre-execute', () => { throw new Error('synthetic policy unavailable'); });
      if (branch === 'post-block') ctx.on('tools/post-execute', () => ({ kind: 'block', feedback: 'synthetic policy block' }));
      if (branch === 'already-canceled') controller.abort();
      const saved = [];
      const notices = [];
      ctx.on(hook, (exec, result) => { saved.push(result); });
      ctx.on('tools/result', (exec, result) => { notices.push(result); });
      const result = await call(ctx, branch === 'unknown' ? 'missing-tool' : 'synthetic-tool', {
        signal: controller.signal, ...(branch === 'invalid-args' ? { arguments: { invalid: 1n } } : {}),
      });
      assert.deepEqual(saved, [result], branch);
      assert.deepEqual(notices, [result], branch);
      assert.equal(result.isError, branch !== 'transformed', branch);
      if (branch === 'transformed') assert.equal(result.content[0].text, 'after final transform🙂');
      assert.equal(effects, ['transformed', 'body-error', 'post-block', 'canceled-in-body'].includes(branch) ? 1 : 0, branch);
    } finally { await ctx.fiber.dispose(); }
  }
});

test('saving failure rejects once, publishes nothing and prevents automatic retry', { timeout: 5000 }, async () => {
  const ctx = await service();
  try {
    let effects = 0;
    let saves = 0;
    let notices = 0;
    ctx.tools.register(fixture('synthetic-tool', async () => { effects++; return [{ type: 'text', text: 'side effect happened' }]; }));
    ctx.on('tools/result', () => { notices++; });
    ctx.on(hook, () => { saves++; throw new Error('synthetic disk full; must not leak this detail'); });
    await assert.rejects(call(ctx), error => error.code === 'EVERNOOK_TOOL_PUBLICATION_FAILED'
      && error.message === 'evernook:tool_publication_failed');
    assert.deepEqual({ effects, saves, notices }, { effects: 1, saves: 1, notices: 0 });
    await assert.rejects(call(ctx), error => error.code === 'EVERNOOK_TOOL_PUBLICATION_FAILED');
    assert.deepEqual({ effects, saves, notices }, { effects: 1, saves: 1, notices: 0 });
  } finally { await ctx.fiber.dispose(); }
});
