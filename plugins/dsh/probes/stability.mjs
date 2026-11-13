// Formal synthetic acceptance on an actual installed dsh profile. Never a daily-profile launcher.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { readFile, writeFile, stat } from 'node:fs/promises';
import { openSync, closeSync, realpathSync, existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, resolve, dirname, relative, isAbsolute, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { disk } from './stability-disk.mjs';

const [setupPath, fixturePath, pilotSeconds] = process.argv.slice(2);
assert.equal(process.platform, 'win32');
const setup = JSON.parse(await readFile(setupPath, 'utf8'));
const root = realpathSync.native(fixturePath);
assert.equal(setup.synthetic, true); assert.ok(setup.profile.startsWith('evernook-stability'));
assert.ok(root.startsWith(realpathSync.native(setup.root) + sep));
const policyText = await readFile(setup.policy, 'utf8');
const driverText = await readFile(import.meta.filename);
const policy = JSON.parse(policyText);
assert.equal(process.versions.node, policy.node);
const seconds = pilotSeconds ? Number(pilotSeconds) : policy.observation_seconds;
assert.ok(Number.isSafeInteger(seconds) && seconds >= 30 && seconds <= policy.observation_seconds);
const fixture = JSON.parse(await readFile(join(root, 'fixture.json'), 'utf8'));
assert.equal(fixture.core_version, policy.core);
const profile = join(setup.home, 'profiles', setup.profile);
const require = createRequire(join(profile, 'package.json'));
const installedEntry = require.resolve('dsh-evernook');
const installedClient = join(dirname(installedEntry), 'client.js');
const packageInfo = JSON.parse(await readFile(join(dirname(dirname(installedEntry)), 'package.json'), 'utf8'));
assert.equal(packageInfo.version, policy.plugin);
const { MemoryClient } = await import(pathToFileURL(installedClient));
const networkLog = join(root, 'network-denied.jsonl');
await writeFile(join(dirname(dirname(setup.python)), 'Lib/site-packages/stability-network-journal.txt'), networkLog);
const preload = resolve(import.meta.dirname, 'stability-network.mjs');
const preloadText = await readFile(preload);
const diskProbe = resolve(import.meta.dirname, 'stability-disk.mjs');
const diskText = await readFile(diskProbe);
await writeFile(join(root, 'policy-frozen.json'), policyText, { flag: 'wx' });
await writeFile(join(root, 'driver-frozen.mjs'), driverText, { flag: 'wx' });
await writeFile(join(root, 'network-frozen.mjs'), preloadText, { flag: 'wx' });
await writeFile(join(root, 'disk-frozen.mjs'), diskText, { flag: 'wx' });
process.env.EVERNOOK_STABILITY_NETWORK_LOG = networkLog;
await import(pathToFileURL(preload));
const env = { ...process.env, DSH_HOME: setup.home, DSH_TELEMETRY_MODE: 'DISABLED', PYTHONPATH: '',
  NODE_OPTIONS: [process.env.NODE_OPTIONS, '--import=' + pathToFileURL(preload).href].filter(Boolean).join(' '),
  NO_PROXY: 'localhost,127.0.0.1,::1' };
const clientConfig = { python: setup.python, config: fixture.config,
  access: join(root, 'runtime/mcp-access-dsh.json'), entry: 'dsh', cwd: root };
let reader = new MemoryClient(clientConfig);
let host; let maintenance; let bootIndex = 0; let origin; let cookie; let csrf;
let observationStarted; let observationEnded; let recovery;
const records = []; const errors = []; const actions = {};
const started = performance.now();
const hash = text => createHash('sha256').update(text).digest('hex');
const networkAttempts = async () => existsSync(networkLog) ? (await readFile(networkLog, 'utf8')).trim().split('\n').filter(Boolean).length : 0;
const hostDisk = await disk(setup.host);
async function save(phase) {
  assert.equal(hash(await readFile(import.meta.filename)), hash(driverText), 'driver changed during observation');
  assert.equal(hash(await readFile(preload)), hash(preloadText), 'network preload changed during observation');
  assert.equal(hash(await readFile(diskProbe)), hash(diskText), 'disk probe changed during observation');
  const usedDisk = hostDisk + await disk(setup.root);
  if (usedDisk > policy.experiment_disk_bytes_max && !errors.includes('experiment_disk_limit')) errors.push('experiment_disk_limit');
  const report = { phase, pilot: Boolean(pilotSeconds), requested_seconds: seconds,
    elapsed_seconds: (performance.now() - started) / 1000,
    observation_seconds: observationStarted ? ((observationEnded ?? performance.now()) - observationStarted) / 1000 : 0,
    records, actions, errors, network_attempts: await networkAttempts(), logical_disk_bytes: usedDisk,
    policy_sha256: hash(policyText), driver_sha256: hash(driverText), network_preload_sha256: hash(preloadText), disk_probe_sha256: hash(diskText),
    installed_client: installedClient, installed_client_sha256: hash(await readFile(installedClient)),
    scope: 'Actual isolated dsh/native tools and installed wheel; synthetic provider, application network denial, no physical OS or real model admission.' };
  await writeFile(join(root, 'stability.json'), JSON.stringify(report, null, 2));
}
function owned(child) { return { child, done: once(child, 'exit') }; }
async function stop(handle) {
  if (!handle || handle.child.exitCode !== null) return;
  assert.ok(handle.child.pid);
  const result = spawnSync(join(process.env.SystemRoot, 'System32/taskkill.exe'), ['/PID', String(handle.child.pid), '/T', '/F'],
    { windowsHide: true, timeout: 10000, stdio: 'ignore' });
  assert.equal(result.status, 0, 'owned process tree did not stop');
  await handle.done;
}
async function post(path, body, protectedRoute = false) {
  const response = await fetch(origin + path, { method: 'POST', signal: AbortSignal.timeout(25000), headers: {
    Origin: origin, Cookie: cookie, 'Content-Type': 'application/json', ...(protectedRoute ? { 'X-EverNook-Csrf': csrf } : {}) },
    body: JSON.stringify(body) });
  const value = await response.json();
  if (response.status !== 200) {
    await writeFile(join(root, 'api-failure.json'), JSON.stringify({ status: response.status, path, action: body.action ?? body.case, value }, null, 2));
  }
  assert.equal(response.status, 200, /^[\w:-]{1,100}$/.test(value.error ?? '') ? value.error : 'synthetic API response failed');
  return value;
}
const manage = body => post('/api/evernook/manage', body, true).then(value => value.result);
const counters = () => post('/api/evernook-synthetic94/probe', { action: 'recovery_counters' });
async function startHost() {
  const log = join(root, 'host-' + bootIndex++ + '.log'); const descriptor = openSync(log, 'wx');
  host = owned(spawn(process.execPath, [join(setup.checkout, 'plugins/dsh/host/launch.mjs'), setup.host,
    '--profile', setup.profile, '--port', String(setup.port), '--no-open'], { env, cwd: root, windowsHide: true,
    stdio: ['ignore', descriptor, descriptor] })); closeSync(descriptor);
  let launch; const deadline = performance.now() + 60000;
  while (!launch && performance.now() < deadline) {
    assert.equal(host.child.exitCode, null, 'owned host terminated during startup');
    launch = (await readFile(log, 'utf8')).match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
    if (!launch) await delay(250);
  }
  assert.ok(launch, 'owned host startup deadline exceeded'); origin = new URL(launch).origin;
  const response = await fetch(launch, { redirect: 'manual', signal: AbortSignal.timeout(10000) });
  cookie = response.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
  const bootstrap = await post('/api/evernook/manage', { action: 'bootstrap' });
  assert.equal(bootstrap.configured, true); assert.equal(bootstrap.stream_mode, 'durable_tools');
  assert.equal(bootstrap.recovery_supported, true); csrf = bootstrap.csrf;
}
async function native(name) {
  const result = await post('/api/evernook-synthetic94/probe', { case: name });
  assert.equal(result.synthetic, true); assert.equal(result.timed_out, false);
  return result;
}
async function checkedNative(name) {
  const result = await native(name);
  await writeFile(join(root, 'last-native.json'), JSON.stringify(result, null, 2));
  assert.deepEqual(result.errors, []); assert.deepEqual(result.blocked, []);
  assert.equal(result.requests, result.expected_requests);
  const returned = result.results.find(value => value.type === 'tool/result'); assert.ok(returned);
  assert.equal(Boolean(returned.data.message.isError), result.expected_error);
  const call = result.calls.find(value => value.type === 'tool/call'); assert.ok(call && Number.isSafeInteger(call.seq));
  const source = { entry: 'dsh', session: result.session, branch: result.session,
    event: `tool-publish-v1:${call.seq}:${hash(call.data.callId)}:result` };
  const stored = await reader.call('read_source', { source });
  assert.equal((await reader.readCapture(stored.receipt.message_id)).complete, true);
  assert.deepEqual(JSON.parse(stored.event.text).result.content, returned.data.message.content);
  if (result.expected_preface) {
    assert.equal(result.live_prefix_observed, true);
    let offset = 0; let found = false;
    do {
      const attempts = await reader.streamAttempts('evernook_dsh_text_stream_v1', 'dsh', result.session, offset);
      const pages = await Promise.all(attempts.sources.map(source => reader.readStream({ ...source, event: source.event.replace(/:start$/, ':display') })));
      found = pages.some(page => page.complete && page.text === result.expected_preface);
      if (attempts.next_offset === null) break;
      assert.ok(attempts.next_offset > offset, 'stream discovery cursor did not advance');
      offset = attempts.next_offset;
    } while (!found);
    assert.ok(found, 'complete published prefix was not found in durable stream pages');
  }
  if (result.expected_reasoning) { assert.equal(result.reasoning_held, true); assert.equal(result.live_reasoning, result.expected_reasoning); }
  return result;
}
async function recoveryUnchanged() {
  const before = await counters();
  const result = await manage({ action: 'recovery_confirm', session_id: recovery.session, proposal_id: recovery.proposal_id, fingerprint: recovery.fingerprint });
  assert.equal(result.target_session, recovery.target); assert.equal(result.state, 'complete');
  assert.deepEqual(await counters(), before);
  assert.equal(await readFile(recovery.file, 'utf8'), recovery.content);
  assert.equal((await stat(recovery.file)).mtimeMs, recovery.mtime);
}
try {
  await save('startup'); await startHost(); await reader.connect();
  const status = await reader.call('status', {}); assert.deepEqual(status.capabilities.scopes, ['capture', 'recall']);
  const failed = await native('recovery-write'); assert.equal(failed.requests, 1);
  assert.equal(failed.results.length, 0); assert.equal(failed.blocked.length, 1); assert.equal(failed.errors.length, 1);
  const file = resolve(failed.workspace, failed.expected_file.path);
  const inside = relative(realpathSync.native(setup.root), file);
  assert.ok(inside && !inside.startsWith('..' + sep) && !isAbsolute(inside));
  assert.equal(await readFile(file, 'utf8'), failed.expected_file.content);
  const preview = await manage({ action: 'recovery_preview', session_id: failed.session });
  const confirmed = await manage({ action: 'recovery_confirm', session_id: failed.session, proposal_id: preview.proposal_id, fingerprint: preview.fingerprint });
  recovery = { session: failed.session, proposal_id: preview.proposal_id, fingerprint: preview.fingerprint,
    target: confirmed.target_session, file, content: failed.expected_file.content, mtime: (await stat(file)).mtimeMs };
  const plan = JSON.parse(await readFile(setup.plan, 'utf8'));
  plan.cases['readonly-check'] = { preset: plan.cases.read.preset, resumeSession: recovery.target, tool: 'write',
    args: { file_path: '$WORK/readonly-must-not-exist.txt', content: 'synthetic forbidden write' }, expectError: true };
  await writeFile(setup.plan, JSON.stringify(plan)); await recoveryUnchanged(); actions.prepared_recovery = true;
  maintenance = owned(spawn(setup.python, [join(setup.checkout, 'probes/installed/fixture.py'), 'maintain', '--output', root,
    '--seconds', String(seconds + 60)], { env, cwd: root, windowsHide: true, stdio: 'ignore' }));
  observationStarted = performance.now();
  for (let index = 0; performance.now() - observationStarted < seconds * 1000; index++) {
    const tick = performance.now();
    const result = await checkedNative(index % 2 ? 'reasoning-read' : 'mixed-read');
    records.push({ index, elapsed_seconds: (performance.now() - observationStarted) / 1000,
      roundtrip_ms: performance.now() - tick, session: result.session, native_results: result.results.length });
    const elapsed = performance.now() - observationStarted;
    if (!actions.cancellation && elapsed >= seconds * 1000 / 6) { await checkedNative('cancel'); actions.cancellation = true; }
    if (!actions.permission_denial && elapsed >= seconds * 1000 / 3) { await checkedNative('denied'); actions.permission_denial = true; }
    if (!actions.host_restart && elapsed >= seconds * 1000 / 2) {
      await stop(host); await startHost(); await recoveryUnchanged(); actions.host_restart = true;
    }
    if (!actions.client_restart && elapsed >= seconds * 2000 / 3) {
      await reader.close(); reader = new MemoryClient(clientConfig); await reader.connect();
      assert.equal((await reader.read(fixture.original_id)).event.text, fixture.original);
      await recoveryUnchanged(); actions.client_restart = true;
    }
    if (!actions.readonly_denial && elapsed >= seconds * 5000 / 6) {
      const before = await counters(); const denied = await checkedNative('readonly-check');
      assert.equal(denied.results.find(value => value.type === 'tool/result').data.error.code, 'EVERNOOK_RECOVERY_READ_ONLY');
      assert.ok(denied.trace.some(value => value.agent_loop && value.recovery_context));
      assert.equal(existsSync(join(denied.workspace, 'readonly-must-not-exist.txt')), false);
      assert.equal((await counters()).tool_executions, before.tool_executions);
      await recoveryUnchanged(); actions.readonly_denial = true;
    }
    assert.equal(await networkAttempts(), 0, 'unexpected external network attempt');
    assert.equal(maintenance.child.exitCode, null, 'owned maintenance terminated unexpectedly');
    await save('running'); assert.deepEqual(errors, []);
    await delay(Math.max(0, observationStarted + (index + 1) * policy.turn_interval_seconds * 1000 - performance.now()));
  }
  observationEnded = performance.now();
  if (!pilotSeconds) assert.ok(records.length >= policy.observation_seconds / policy.turn_interval_seconds, 'formal foreground rounds missing');
  assert.ok(['prepared_recovery', 'cancellation', 'permission_denial', 'host_restart', 'client_restart', 'readonly_denial'].every(key => actions[key]));
  await recoveryUnchanged(); actions.completed_window = true;
} catch (error) {
  errors.push(String(error?.message ?? 'stability_failed').replace(/https?:\/\/\S+/g, '[url]').slice(0, 200));
} finally {
  observationEnded ??= performance.now();
  await reader.close().catch(() => errors.push('reader_cleanup_failed'));
  await writeFile(join(root, 'maintenance-stop'), 'stop\n');
  if (maintenance) {
    await Promise.race([maintenance.done, delay(5000)]);
    if (maintenance.child.exitCode === null) await stop(maintenance).catch(() => errors.push('maintenance_cleanup_failed'));
    else if (maintenance.child.exitCode !== 0) errors.push('maintenance_failed');
  }
  await stop(host).catch(() => errors.push('host_cleanup_failed'));
  await save(errors.length ? 'failed' : 'complete');
}
console.log(JSON.stringify({ pilot: Boolean(pilotSeconds), rounds: records.length, errors: errors.length,
  network_attempts: await networkAttempts(), passed: errors.length === 0 }));
process.exitCode = errors.length || await networkAttempts() ? 1 : 0;
