import { Socket } from 'node:net';

/** Process-level Node egress denial, not a change to the user's OS network settings. */
export function denyNetwork() {
  const attempts: string[] = [];
  const originalFetch = globalThis.fetch;
  const originalConnect = Socket.prototype.connect;
  globalThis.fetch = async () => {
    attempts.push('fetch');
    throw new Error('R0 offline run denied fetch');
  };
  Socket.prototype.connect = function () {
    attempts.push('socket.connect');
    throw new Error('R0 offline run denied socket.connect');
  } as typeof Socket.prototype.connect;
  return {
    attempts,
    restore() { globalThis.fetch = originalFetch; Socket.prototype.connect = originalConnect; },
  };
}
