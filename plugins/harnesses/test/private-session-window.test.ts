import assert from 'node:assert/strict';
import test from 'node:test';
import childProcess, { type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { PassThrough } from 'node:stream';
import { PrivateSessionWindow } from '../src/private-session-window.ts';

test('private session handles bounded receipts, retries, crashes and hostile output without leaking content', async () => {
  const original = childProcess.spawn;
  const children: (EventEmitter & { stdin: PassThrough; stdout: PassThrough; kill: () => boolean })[] = [];
  let args: unknown[] = [];
  childProcess.spawn = ((...values: unknown[]) => {
    args = values;
    const child = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), kill: () => { child.emit('close', null); return true; } });
    children.push(child);
    queueMicrotask(() => child.emit('spawn'));
    return child as unknown as ChildProcess;
  }) as unknown as typeof childProcess.spawn;
  syncBuiltinESMExports();
  try {
    const window = new PrivateSessionWindow({ python: 'trusted-python', config: 'trusted-config', access: 'trusted-access', entry: 'dsh' });
    const session = '00000000-0000-4000-8000-000000000001';
    const event = '00000000-0000-4000-8000-000000000002';
    await window.open();
    assert.equal(window.blocksOtherWindow(), true);
    assert.deepEqual(args, ['trusted-python', ['-B', '-m', 'evernook.private_window_worker', 'service-private-session', '--config', 'trusted-config', '--access', 'trusted-access', '--entry', 'dsh'], { windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'], env: { ...process.env, EVERNOOK_WORKER_PARENT_PID: String(process.pid), EVERNOOK_WORKER_LIFETIME_PIPE: '1' } }]);
    await assert.rejects(window.open(), /private_window_unavailable/);
    children[0].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'outcome_unknown', saved_count: 2, session_id: session, pending_event_id: event, sequence: 2 } }));
    children[0].emit('close', 0);
    assert.deepEqual(window.status(), { state: 'outcome_unknown', saved_count: 2, session_id: session, pending_event_id: event, sequence: 2 });
    await window.open(true);
    assert.deepEqual((args[1] as string[]).slice(-6), ['--session-id', session, '--event-id', event, '--sequence', '2']);
    children[1].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'outcome_unknown', saved_count: 0, session_id: session, pending_event_id: event, sequence: 2 } }));
    children[1].emit('close', 0);
    assert.equal(window.status().pending_event_id, event);
    await window.open(true);
    children[2].emit('close', 1);
    assert.deepEqual(window.status(), { state: 'outcome_unknown' });
    await assert.rejects(window.open(true), /private_window_unavailable/);
    await assert.rejects(window.open(), /private_window_unavailable/);
    window.abandon();
    await window.open();
    children[3].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'saved', saved_count: 1, session_id: session, text: 'synthetic-should-not-escape' } }));
    children[3].emit('close', 0);
    assert.deepEqual(window.status(), { state: 'outcome_unknown' });
    assert.ok(!JSON.stringify(window.status()).includes('synthetic-should-not-escape'));
    window.abandon();
    await window.open();
    children[4].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'saved', saved_count: 2, session_id: session } }));
    children[4].emit('close', 0);
    assert.deepEqual(window.status(), { state: 'saved', saved_count: 2, session_id: session });
    assert.equal(window.blocksOtherWindow(), false);
    window.close();
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});
