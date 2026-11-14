import assert from 'node:assert/strict';
import test from 'node:test';
import childProcess, { type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { PassThrough } from 'node:stream';
import { PrivateWindow } from '../src/private-window.ts';

test('private window uses only trusted launch arguments, rejects concurrent starts and stops on close', async () => {
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
    const window = new PrivateWindow({ python: 'trusted-python', config: 'trusted-config', access: 'trusted-access', entry: 'dsh' });
    await window.open();
    const initial = window.status();
    assert.equal(initial.state, 'requested');
    assert.deepEqual(args, ['trusted-python', ['-B', '-m', 'evernook.private_window_worker', 'service-private-window', '--config', 'trusted-config', '--access', 'trusted-access', '--entry', 'dsh', '--session-id', initial.session_id, '--event-id', initial.event_id], { windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'], env: { ...process.env, EVERNOOK_WORKER_PARENT_PID: String(process.pid), EVERNOOK_WORKER_LIFETIME_PIPE: '1' } }]);
    await assert.rejects(window.open(), /private_window_unavailable/);
    children[0].emit('close', 1);
    assert.equal(window.status().state, 'outcome_unknown');
    await assert.rejects(window.open(), /private_window_unavailable/);
    await window.open(true);
    assert.equal(window.status().session_id, initial.session_id);
    assert.equal(window.status().event_id, initial.event_id);
    children[1].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'canceled', session_id: initial.session_id, event_id: initial.event_id } }));
    children[1].emit('close', 0);
    assert.equal(window.status().state, 'outcome_unknown');
    await window.open(true);
    children[2].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'saved', session_id: initial.session_id, event_id: initial.event_id } }));
    children[2].emit('close', 0);
    assert.equal(window.status().state, 'saved');
    await assert.rejects(window.open(true), /private_window_unavailable/);
    await window.open();
    children[3].stdout.write(JSON.stringify({ contract_version: 1, ok: true, result: { state: 'saved', session_id: window.status().session_id, event_id: window.status().event_id, text: 'synthetic-should-not-escape' } }));
    children[3].emit('close', 0);
    assert.equal(window.status().state, 'outcome_unknown');
    assert.ok(!JSON.stringify(window.status()).includes('synthetic-should-not-escape'));
    window.abandon();
    assert.equal(window.status().state, 'not_started');
    await window.open();
    window.close();
    await assert.rejects(window.open(), /private_window_unavailable/);
    assert.equal(children.length, 5);
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});

test('spawn failure fails closed and allows a later retry', async () => {
  const window = new PrivateWindow({ python: 'Z:/does-not-exist/evernook-python.exe', config: 'x', access: 'x', entry: 'dsh' });
  await assert.rejects(window.open(), /private_window_unavailable/);
  await assert.rejects(window.open(), /private_window_unavailable/);
  window.close();
});

test('a process error after launch preserves the unknown source for retry', async () => {
  const original = childProcess.spawn;
  let child: EventEmitter & { stdin: PassThrough; stdout: PassThrough; kill: () => boolean };
  childProcess.spawn = (() => {
    child = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), kill: () => true });
    queueMicrotask(() => child.emit('spawn'));
    return child as unknown as ChildProcess;
  }) as unknown as typeof childProcess.spawn;
  syncBuiltinESMExports();
  try {
    const window = new PrivateWindow({ python: 'python', config: 'config', access: 'access', entry: 'dsh' });
    await window.open();
    const initial = window.status();
    child!.emit('error', new Error('synthetic'));
    assert.equal(window.status().state, 'outcome_unknown');
    assert.equal(window.status().event_id, initial.event_id);
    await window.open(true);
    window.close();
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});
