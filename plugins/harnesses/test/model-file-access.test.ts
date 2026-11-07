import assert from 'node:assert/strict';
import { join, resolve } from 'node:path';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import test from 'node:test';
import { withModelFileAccess, type ModelFileEnvironment } from '../probes/model-file-access.ts';
import { LocalQwenChat } from '../probes/local-qwen.ts';

const model = resolve(process.env.EVERNOOK_TEST_OUTPUT ?? process.env.TEMP!, 'synthetic-pinned-model');
const environment = (): ModelFileEnvironment => ({ useCustomCache: false, customCache: null,
  remoteHost: 'https://huggingface.co/', remotePathTemplate: '{model}/resolve/{revision}/' });

test('verified path cache resolves only the two graph aliases and restores previous cache state', async () => {
  const env = environment();
  const previous = { async match() { return undefined; }, async put() {} };
  env.useCustomCache = true; env.customCache = previous;
  const result = await withModelFileAccess(env, model, 'verified-local-path-v1', async () => {
    assert.equal(env.useCustomCache, true); assert.ok(env.customCache);
    assert.equal(await env.customCache.match(model + '/onnx/model_q4.onnx'), join(model, 'onnx/model_q4.onnx'));
    const alias = 'https://huggingface.co/' + model.replace(/^\//, '') + '/resolve/main/onnx/model_q4.onnx';
    assert.equal(await env.customCache.match(alias), join(model, 'onnx/model_q4.onnx'));
    for (const key of ['config.json', model + '/onnx/other.onnx', model + '/onnx/model_q4.onnx?x=1', 'https://other.invalid/model_q4.onnx']) {
      assert.equal(await env.customCache.match(key), undefined);
    }
    await assert.rejects(env.customCache.put('unexpected', new Response()), /model_path_cache_read_only/);
    return 'loaded';
  });
  assert.deepEqual(result, { value: 'loaded', pathHits: 2 });
  assert.equal(env.useCustomCache, true); assert.equal(env.customCache, previous);
});

test('failed load restores cache and a concurrent default load waits for the temporary cache to leave', async () => {
  const env = environment();
  const entered = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>();
  const order: string[] = [];
  const first = withModelFileAccess(env, model, 'verified-local-path-v1', async () => {
    order.push('cached'); entered.resolve(); await release.promise;
    throw new Error('synthetic load failed');
  });
  const rejected = assert.rejects(first, /synthetic load failed/);
  await entered.promise;
  const second = withModelFileAccess(env, model, 'library-default', async () => {
    order.push('default'); assert.equal(env.useCustomCache, false); assert.equal(env.customCache, null);
    return 7;
  });
  assert.deepEqual(order, ['cached']); release.resolve();
  await rejected; assert.deepEqual(await second, { value: 7, pathHits: 0 });
  assert.deepEqual(order, ['cached', 'default']); assert.equal(env.customCache, null);
});

test('invalid verified path is refused and leaves the load queue usable', async () => {
  const env = environment();
  await assert.rejects(withModelFileAccess(env, 'relative', 'verified-local-path-v1', async () => assert.fail()), /absolute_model_path_required/);
  assert.equal(env.useCustomCache, false); assert.equal(env.customCache, null);
  assert.deepEqual(await withModelFileAccess(env, model, 'library-default', async () => 1), { value: 1, pathHits: 0 });
});

test('concurrent verified loads never resolve one model through another model path', async () => {
  const env = environment(); const entered = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>();
  const other = model + '-other';
  const first = withModelFileAccess(env, model, 'verified-local-path-v1', async () => {
    entered.resolve(); await release.promise; assert.ok(env.customCache);
    assert.equal(await env.customCache.match(other + '/onnx/model_q4.onnx'), undefined);
    return 'first';
  });
  await entered.promise;
  const second = withModelFileAccess(env, other, 'verified-local-path-v1', async () => {
    assert.ok(env.customCache);
    assert.equal(await env.customCache.match(model + '/onnx/model_q4.onnx'), undefined);
    assert.equal(await env.customCache.match(other + '/onnx/model_q4.onnx'), join(other, 'onnx/model_q4.onnx'));
    return 'second';
  });
  release.resolve(); assert.equal((await first).value, 'first'); assert.equal((await second).value, 'second');
  assert.equal(env.useCustomCache, false); assert.equal(env.customCache, null);
});

test('malformed explicit file access is rejected before model assets or dependencies are read', async () => {
  const output = resolve(process.env.EVERNOOK_TEST_OUTPUT ?? process.env.TEMP!, 'model-file-profile-tests');
  await mkdir(output, { recursive: true }); const root = await mkdtemp(join(output, 'profiles-'));
  for (const value of [null, false, 1, {}, 'unsupported']) {
    const path = join(root, 'profile.json');
    await writeFile(path, JSON.stringify({ profile: 'qwen3-0.6b-last-hidden-experimental-v1',
      model_dir: join(root, 'must-not-be-read'), transformers_dir: join(root, 'must-not-be-imported'), file_access: value }));
    await assert.rejects(LocalQwenChat.create(path), /model_file_access_mismatch/);
  }
});
