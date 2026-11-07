// Real installed dsh owner/API fence checks. Only use an isolated synthetic profile.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
const [logPath, reportPath, mode] = process.argv.slice(2);
const launch = readFileSync(logPath, 'utf8').match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
assert.ok(launch, 'No isolated loopback launch URL');
const base = new URL(launch).origin;
const initial = await fetch(launch, { redirect: 'manual' });
const cookie = initial.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
assert.ok(cookie, 'Owner exchange did not issue a cookie');
const send = (payload, extra = {}) => fetch(base + '/api/evernook/manage', { method: 'POST',
  headers: { Origin: base, 'Content-Type': 'application/json', Cookie: cookie, ...extra }, body: JSON.stringify(payload) });
if (mode === 'absent') {
  const response = await send({ action: 'bootstrap' });
  assert.equal(response.status, 404);
  await response.arrayBuffer();
  writeFileSync(reportPath, JSON.stringify({ synthetic: true, uninstalled_route_status: response.status }, null, 2));
  console.log(JSON.stringify({ uninstalled: true, route_status: response.status }));
} else {
const observations = [];
const check = (name, actual, expected) => { assert.equal(actual, expected, name); observations.push({ name, actual, expected }); };
check('unauthenticated', (await send({ action: 'bootstrap' }, { Cookie: '' })).status, 401);
check('cross_origin', (await send({ action: 'bootstrap' }, { Origin: 'https://hostile.invalid' })).status, 403);
const bootstrap = await send({ action: 'bootstrap' });
check('owner_bootstrap', bootstrap.status, 200);
const { csrf, configured } = await bootstrap.json();
assert.equal(configured, true);
check('missing_csrf', (await send({ action: 'read', view: 'status' })).status, 403);
check('malformed_csrf', (await send({ action: 'read', view: 'status' }, { 'X-EverNook-Csrf': '!'.repeat(64) })).status, 403);
const status = await send({ action: 'read', view: 'status' }, { 'X-EverNook-Csrf': csrf });
check('owner_management', status.status, 200);
check('no_store', status.headers.get('cache-control'), 'no-store');
const body = await status.json();
check('preview_not_daily', body.result.daily_admitted, false);
check('forged_confirmation', (await send({ action: 'confirm', proposal_id: 'forged',
  operation: { kind: 'source_suppress', message_id: 'forged' } }, { 'X-EverNook-Csrf': csrf })).status, 400);
writeFileSync(reportPath, JSON.stringify({ synthetic: true, observations }, null, 2));
console.log(JSON.stringify({ checks: observations.length, passed: true }));
}
