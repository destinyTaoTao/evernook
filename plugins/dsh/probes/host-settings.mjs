// Exercise the real settings/config-reload path on a disposable synthetic host only.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
async function main() {
const launch = readFileSync(process.argv[2], 'utf8').match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
assert.ok(launch, 'Expected isolated loopback launch log');
const base = new URL(launch).origin;
const initial = await fetch(launch, { redirect: 'manual' });
const cookie = initial.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
assert.ok(cookie, 'Owner authentication failed');
async function call(method, args) {
  const response = await fetch(base + '/api/' + method, { method: 'POST',
    headers: { Origin: base, Cookie: cookie, 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'client-request', rpcId: randomUUID(), method, payload: { args } }),
  });
  assert.equal(response.status, 200);
  return (await response.json()).result;
}
const before = await call('settings/describe', {});
assert.equal(before.ok, true);
const settings = before.value.namespaces.find(value => value.ns === 'ui-settings-general');
assert.ok(settings);
const saved = await call('settings/mutate', { ns: settings.ns, expectedRevision: settings.revision,
  ops: [{ op: 'set', path: ['welcomeNoticeVersion'], value: '2026-09-28.1' }] });
// Print only the diagnostic signal. Never print cookies, tokens or settings documents.
console.log(JSON.stringify({ settings_saved: saved.ok, error: saved.error?.code, message: saved.error?.message }));
assert.equal(saved.ok, true, 'Welcome acknowledgement could not survive the host config reload');
const after = await call('settings/describe', {});
assert.equal(after.value.namespaces.find(value => value.ns === settings.ns).value.welcomeNoticeVersion, '2026-09-28.1');
}
await main().catch(error => { console.error(error.message); process.exitCode = 1; });
