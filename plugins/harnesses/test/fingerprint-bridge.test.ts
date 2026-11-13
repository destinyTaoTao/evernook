import assert from 'node:assert/strict';
import test from 'node:test';
import childProcess, { type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { PassThrough } from 'node:stream';
import { FingerprintBridge } from '../src/fingerprint-bridge.ts';

test('bounded bridge returns only checked tokens, handles cancellation and fails pending requests on crash', async () => {
  const original = childProcess.spawn;
  const children: (EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough; kill(): boolean })[] = [];
  childProcess.spawn = (() => {
    const child = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
      kill: () => { child.emit('close'); return true; } });
    children.push(child);
    return child as unknown as ChildProcessWithoutNullStreams;
  }) as unknown as typeof childProcess.spawn;
  syncBuiltinESMExports();
  try {
    const bridge = new FingerprintBridge({ python: 'python', config: 'config', access: 'access' });
    const first = bridge.request([{ view: 'timeline' }]);
    const request = JSON.parse(children[0].stdin.read().toString());
    children[0].stdout.write(JSON.stringify({ id: request.id, tokens: ['a'.repeat(64)] }) + '\n');
    assert.deepEqual(await first, { tokens: ['a'.repeat(64)] });
    const controller = new AbortController();
    const canceled = bridge.request([{ view: 'timeline' }], controller.signal);
    const sent = JSON.parse(children[0].stdin.read().toString());
    controller.abort();
    await assert.rejects(canceled, /fingerprint_unavailable/);
    assert.deepEqual(JSON.parse(children[0].stdin.read().toString()), { id: sent.id, cancel: true });
    const crashed = bridge.request([{ view: 'timeline' }]);
    children[0].emit('close');
    await assert.rejects(crashed, /fingerprint_unavailable/);
    const invalid = bridge.request([{ view: 'timeline' }]);
    const next = JSON.parse(children[1].stdin.read().toString());
    children[1].stdout.write(JSON.stringify({ id: next.id, tokens: ['not-a-token'] }) + '\n');
    await assert.rejects(invalid, /fingerprint_unavailable/);
    bridge.close();
    await assert.rejects(bridge.request([{ view: 'timeline' }]), /fingerprint_unavailable/);
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});

test('bridge caps concurrent requests and rejects them all on close', async () => {
  const original = childProcess.spawn;
  childProcess.spawn = (() => {
    const child = Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
      kill: () => { child.emit('close'); return true; } });
    return child as unknown as ChildProcessWithoutNullStreams;
  }) as unknown as typeof childProcess.spawn;
  syncBuiltinESMExports();
  try {
    const bridge = new FingerprintBridge({ python: 'python', config: 'config', access: 'access' });
    const pending = Array.from({ length: 4 }, () => bridge.request([{ view: 'timeline' }]));
    await assert.rejects(bridge.request([{ view: 'timeline' }]), /fingerprint_unavailable/);
    bridge.close();
    assert.ok((await Promise.allSettled(pending)).every(item => item.status === 'rejected'));
  } finally { childProcess.spawn = original; syncBuiltinESMExports(); }
});

test('installed worker handles parallel authorized requests and rejects capture-only identity',
  { skip: !process.env.EVERNOOK_FINGERPRINT_CONFIG }, async () => {
    const config = { python: process.env.EVERNOOK_TEST_PYTHON!, config: process.env.EVERNOOK_FINGERPRINT_CONFIG!,
      access: process.env.EVERNOOK_FINGERPRINT_ACCESS! };
    const bridge = new FingerprintBridge(config);
    const ordinary = new FingerprintBridge({ ...config, access: process.env.EVERNOOK_FINGERPRINT_ORDINARY! });
    try {
      const values = await Promise.all(Array.from({ length: 4 }, () => bridge.request([{ view: 'timeline' }])));
      assert.equal(values.length, 4);
      assert.ok(values.every(value => Array.isArray(value.tokens) && value.tokens.length === 1));
      await assert.rejects(ordinary.request([{ view: 'timeline' }]), /fingerprint_unavailable/);
    } finally { bridge.close(); ordinary.close(); }
  });
