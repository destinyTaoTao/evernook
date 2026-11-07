// Real isolated owner/API recovery. Credentials stay in the launch log, never stdout or reports.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, statSync } from 'node:fs';
import { resolve, relative, isAbsolute, sep } from 'node:path';
const [logPath, reportPath, mode = 'create'] = process.argv.slice(2);
const launch = readFileSync(logPath, 'utf8').match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
assert.ok(launch);
const base = new URL(launch).origin;
const initial = await fetch(launch, { redirect: 'manual' });
const cookie = initial.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
async function post(path, body, csrf) {
  const response = await fetch(base + path, { method: 'POST', headers: { Origin: base, Cookie: cookie,
    'Content-Type': 'application/json', ...(csrf ? { 'X-EverNook-Csrf': csrf } : {}) }, body: JSON.stringify(body) });
  return { status: response.status, value: await response.json() };
}
const probe = body => post('/api/evernook-synthetic94/probe', body);
const boot = await post('/api/evernook/manage', { action: 'bootstrap' });
assert.equal(boot.status, 200); assert.equal(boot.value.recovery_supported, true);
const manage = body => post('/api/evernook/manage', body, boot.value.csrf);
let report;
if (mode === 'create') {
  const failed = await probe({ case: 'recovery-write' }); assert.equal(failed.status, 200);
  const source = failed.value;
  assert.equal(source.synthetic, true); assert.equal(source.requests, 1);
  assert.equal(source.blocked.length, 1); assert.equal(source.results.length, 0);
  assert.equal(source.errors.length, 1); assert.equal(source.timed_out, false);
  const file = resolve(source.workspace, source.expected_file.path);
  const within = relative(source.workspace, file);
  assert.ok(within && !within.startsWith('..' + sep) && !isAbsolute(within));
  assert.equal(readFileSync(file, 'utf8'), source.expected_file.content);
  report = { synthetic: true, source_session: source.session, file, content: source.expected_file.content, mtime: statSync(file).mtimeMs };
  writeFileSync(reportPath, JSON.stringify(report, null, 2));
  const canceled = await manage({ action: 'recovery_preview', session_id: source.session }); assert.equal(canceled.status, 200, JSON.stringify(canceled.value));
  assert.equal((await manage({ action: 'recovery_cancel', proposal_id: canceled.value.result.proposal_id })).status, 200);
  const rejected = await manage({ action: 'recovery_confirm', session_id: source.session,
    proposal_id: canceled.value.result.proposal_id, fingerprint: canceled.value.result.fingerprint });
  assert.equal(rejected.status, 400); assert.equal(rejected.value.error, 'recovery_canceled');
} else { assert.equal(mode, 'restart'); report = JSON.parse(readFileSync(reportPath, 'utf8')); }
const view = await manage({ action: 'recovery_read', session_id: report.source_session });
assert.equal(view.status, 200); assert.equal(view.value.result.blocked, true);
assert.equal(view.value.result.calls[0].result, 'complete');
const counters = (await probe({ action: 'recovery_counters' })).value;
if (mode === 'create') {
  const preview = await manage({ action: 'recovery_preview', session_id: report.source_session }); assert.equal(preview.status, 200);
  report.proposal_id = preview.value.result.proposal_id; report.fingerprint = preview.value.result.fingerprint;
}
const confirm = { action: 'recovery_confirm', session_id: report.source_session, proposal_id: report.proposal_id, fingerprint: report.fingerprint };
assert.equal((await post('/api/evernook/manage', confirm)).status, 403);
const [first, repeated] = await Promise.all([manage(confirm), manage(confirm)]);
assert.equal(first.status, 200); assert.equal(repeated.status, 200); assert.deepEqual(first.value, repeated.value);
assert.equal(first.value.result.read_only, true); assert.equal(first.value.result.state, 'complete');
if (report.target_session) assert.equal(first.value.result.target_session, report.target_session);
report.target_session = first.value.result.target_session;
assert.deepEqual((await probe({ action: 'recovery_counters' })).value, counters, 'Recovery invoked a model or a tool');
assert.equal(readFileSync(report.file, 'utf8'), report.content); assert.equal(statSync(report.file).mtimeMs, report.mtime);
const after = await manage({ action: 'recovery_read', session_id: report.source_session });
assert.equal(after.value.result.fingerprint, view.value.result.fingerprint, 'The source log/evidence changed');
report[mode] = { passed: true, no_provider_or_tool: true, original_unchanged: true };
writeFileSync(reportPath, JSON.stringify(report, null, 2));
console.log(JSON.stringify({ mode, passed: true, no_provider_or_tool: true, original_unchanged: true, single_read_only_target: true }));
