import assert from 'node:assert/strict';
import test from 'node:test';
import type { Context } from '@deepseek-ai/cordis';
import { installWeb, type Config } from '../src/dsh-web.ts';

test('web preview rejects cross-site, missing origin, missing CSRF, oversized and disposed requests', async () => {
  let route: { path: string; requestBody: string } | undefined;
  let disposed = false;
  const ctx = { get: () => ({ fetch: { register: (value: typeof route) => {
    route = value; return () => { disposed = true; };
  } } }) } as unknown as Context;
  const config: Config = { python: '', config: '', access: '', maintenanceAccess: '', entry: 'dsh', streamMode: 'durable_text' };
  const web = await installWeb(ctx, config);
  assert.equal(route?.path, '/api/evernook/manage');
  assert.equal(route?.requestBody, 'streaming');
  const request = (body: unknown, headers: Record<string, string> = {}) => new Request('http://dsh.internal/api/evernook/manage', {
    method: 'POST', headers: { host: '127.0.0.1:37849', origin: 'http://127.0.0.1:37849', 'content-type': 'application/json', ...headers }, body: JSON.stringify(body),
  });
  assert.equal((await web.handle(request({ action: 'bootstrap' }, { origin: 'https://hostile.invalid' }))).status, 403);
  assert.equal((await web.handle(request({ action: 'bootstrap' }, { 'sec-fetch-site': 'cross-site' }))).status, 403);
  assert.equal((await web.handle(request({ action: 'bootstrap' }, { origin: '' }))).status, 403);
  assert.equal((await web.handle(request({ action: 'bootstrap' }, { host: 'remote.invalid', origin: 'http://remote.invalid' }))).status, 403);
  assert.equal((await web.handle(request({ action: 'bootstrap' }, { host: 'localhost:37849' }))).status, 403);
  const response = await web.handle(request({ action: 'bootstrap' }));
  const bootstrap = await response.json() as { csrf: string; configured: boolean };
  assert.equal(bootstrap.configured, false);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal((await web.handle(request({ action: 'read', view: 'status' }))).status, 403);
  assert.equal((await web.handle(request({ action: 'read', view: 'status' }, { 'x-evernook-csrf': bootstrap.csrf }))).status, 409);
  assert.equal((await web.handle(request({ action: 'bootstrap', huge: 'a'.repeat(140000) }))).status, 413);
  await web.close();
  assert.equal(disposed, true);
  assert.equal((await web.handle(request({ action: 'bootstrap' }))).status, 503);
});
