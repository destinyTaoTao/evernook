// Preload only in disposable stability processes. No global workstation changes.
import net from 'node:net';
import dns from 'node:dns';
import { appendFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';

const log = process.env.EVERNOOK_STABILITY_NETWORK_LOG;
if (!log) throw new Error('synthetic_network_journal_required');
const local = host => host === undefined || host === 'localhost' || host === '::1' || host === '[::1]'
  || typeof host === 'string' && net.isIP(host) === 4 && host.startsWith('127.');
const deny = kind => {
  appendFileSync(log, JSON.stringify({ runtime: 'node', kind, pid: process.pid, at: Date.now() }) + '\n');
  throw new Error('synthetic_external_network_denied');
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const values = Array.isArray(args[0]) ? args[0] : args;
  const first = values[0];
  if (first && typeof first === 'object') {
    if (first.path !== undefined) {
      if (process.platform === 'win32' && !/^\\\\[.?]\\pipe\\/.test(first.path)) deny('pipe');
    } else if (!local(first.host)) deny('tcp');
  } else if (typeof first === 'number') {
    if (typeof values[1] === 'string' && !local(values[1])) deny('tcp');
  } else deny('unknown_connect');
  return connect.apply(this, args);
};
const lookup = dns.lookup;
dns.lookup = function (host, ...args) {
  if (!local(host)) deny('dns');
  return lookup.call(this, host, ...args);
};
const fetch = globalThis.fetch;
globalThis.fetch = async function (input, init) {
  const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  if (!local(url.hostname)) deny('fetch');
  return fetch(input, init);
};
syncBuiltinESMExports();
