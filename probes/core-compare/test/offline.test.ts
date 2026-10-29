import assert from 'node:assert/strict';
import { Socket } from 'node:net';
import test from 'node:test';
import { denyNetwork } from '../src/offline.ts';

test('the offline guard rejects both fetch and direct sockets before network I/O', async () => {
  const fence = denyNetwork();
  const socket = new Socket();
  try {
    await assert.rejects(fetch('https://example.invalid'), /offline/);
    assert.throws(() => socket.connect({ host: 'example.invalid', port: 443 }), /offline/);
    assert.deepEqual(fence.attempts, ['fetch', 'socket.connect']);
  } finally { socket.destroy(); fence.restore(); }
});
