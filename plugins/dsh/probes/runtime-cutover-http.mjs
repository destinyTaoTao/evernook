// Read only through the actual installed owner's route after a synthetic runtime cutover.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, basename } from 'node:path';
const [logPath, fixturePath, nativePath, recoveryPath, readonlyPath, reportPath] = process.argv.slice(2);
const launch = readFileSync(logPath, 'utf8').match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
assert.ok(launch);
const base = new URL(launch).origin;
const exchange = await fetch(launch, { redirect: 'manual', signal: AbortSignal.timeout(10000) });
const cookie = exchange.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
async function send(body, csrf, path = '/api/evernook/manage') {
  const response = await fetch(base + path, { method: 'POST', signal: AbortSignal.timeout(15000), headers: {
    Origin: base, Cookie: cookie, 'Content-Type': 'application/json', ...(csrf ? { 'X-EverNook-Csrf': csrf } : {}) },
    body: JSON.stringify(body) });
  return { status: response.status, body: await response.json() };
}
const boot = await send({ action: 'bootstrap' }); assert.equal(boot.status, 200);
const manage = body => send(body, boot.body.csrf);
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
const native = JSON.parse(readFileSync(nativePath, 'utf8'));
const recovery = JSON.parse(readFileSync(recoveryPath, 'utf8'));
const blocked = JSON.parse(readFileSync(readonlyPath, 'utf8'));
assert.equal(blocked.session, recovery.target_session);
assert.equal(blocked.expected_readonly, true);
assert.ok(blocked.results.some(value => value.data.error?.code === 'EVERNOOK_RECOVERY_READ_ONLY'));
assert.ok(blocked.trace.some(value => value.agent_loop && value.recovery_context));
assert.ok(basename(blocked.workspace).startsWith('synthetic-work-'));
assert.equal(existsSync(join(blocked.workspace, 'readonly-must-not-exist.txt')), false);
const counters = (await send({ action: 'recovery_counters' }, undefined, '/api/evernook-synthetic94/probe')).body;
assert.equal(counters.tool_executions, 0);
const source = await manage({ action: 'read', view: 'source', message_id: fixture.receipt.message_id });
assert.equal(source.status, 200); assert.equal(source.body.result.text, fixture.event.text);
for (const outcome of Object.values(fixture.controlled)) {
  const unavailable = await manage({ action: 'read', view: 'source', message_id: outcome.target_id });
  assert.equal(unavailable.status, 400); assert.equal(unavailable.body.error, 'source_unavailable');
}
const canceled = await manage({ action: 'confirm', proposal_id: fixture.canceled, operation: fixture.canceled_operation });
assert.equal(canceled.status, 400); assert.equal(canceled.body.error, 'management_preview_canceled');
const timeline = await manage({ action: 'read', view: 'timeline', session_id: native.session, limit: 25 });
assert.equal(timeline.status, 200); assert.ok(timeline.body.result.items.length >= 3);
assert.ok(timeline.body.result.items.every(item => item.source.session === native.session));
const readonly = await manage({ action: 'recovery_read', session_id: recovery.target_session });
assert.equal(readonly.status, 200); assert.equal(readonly.body.result.read_only, true);
const after = (await send({ action: 'recovery_counters' }, undefined, '/api/evernook-synthetic94/probe')).body;
assert.deepEqual(after, counters);
const checks = { synthetic: true, owner_source_exact: true, controls_hidden: true, canceled_rejected: true,
  new_runtime_timeline: true, existing_readonly_target: true, no_provider_or_tool_during_review: true,
  readonly_context_present: true, readonly_write_denied: true, readonly_side_effect_absent: true };
writeFileSync(reportPath, JSON.stringify(checks, null, 2)); console.log(JSON.stringify(checks));
