import assert from 'node:assert/strict';
import test from 'node:test';
import childProcess, { type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { PrivateWindow } from '../src/private-window.ts';

test('private window uses only trusted launch arguments, rejects concurrent starts and stops on close', async () => {
  const original = childProcess.spawn;
  const children: (EventEmitter & { kill: () => boolean })[] = [];
  let args: unknown[] = [];
  childProcess.spawn = ((...values: unknown[]) => {
    args = values;
    const child = Object.assign(new EventEmitter(), { kill: () => { child.emit('close'); return true; } });
    children.push(child);
    queueMicrotask(() => child.emit('spawn'));
    return child as ChildProcess;
  }) as typeof childProcess.spawn;
  syncBuiltinESMExports();
  try {
    const window = new PrivateWindow({ python: 'trusted-python', config: 'trusted-config', access: 'trusted-access', entry: 'dsh' });
    await window.open();
    assert.deepEqual(args, ['trusted-python', ['-B', '-m', 'evernook.cli', 'service-private-window', '--config', 'trusted-config', '--access', 'trusted-access', '--entry', 'dsh'], { windowsHide: true, stdio: 'ignore' }]);
    await assert.rejects(window.open(), /private_window_unavailable/);
    children[0].emit('close');
    await window.open();
    window.close();
    await assert.rejects(window.open(), /private_window_unavailable/);
    assert.equal(children.length, 2);
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});

test('spawn failure fails closed and allows a later retry', async () => {
  const window = new PrivateWindow({ python: 'Z:/does-not-exist/evernook-python.exe', config: 'x', access: 'x', entry: 'dsh' });
  await assert.rejects(window.open(), /private_window_unavailable/);
  await assert.rejects(window.open(), /private_window_unavailable/);
  window.close();
});
