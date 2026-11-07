import assert from 'node:assert/strict';
import test from 'node:test';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

assert.ok(process.env.EVERNOOK_TEST_OUTPUT && process.env.EVERNOOK_TEST_PYTHON);
mkdirSync(process.env.EVERNOOK_TEST_OUTPUT, { recursive: true });
const root = mkdtempSync(join(process.env.EVERNOOK_TEST_OUTPUT, 'network-'));
test('Node denies external fetch/TCP/DNS before connecting while loopback HTTP remains usable', () => {
  const log = join(root, 'node.jsonl');
  execFileSync(process.execPath, ['--import', pathToFileURL(resolve(import.meta.dirname, '../stability-network.mjs')).href,
    '--input-type=module', '-e', `
      import assert from 'node:assert/strict'; import net from 'node:net'; import dns from 'node:dns'; import {createServer} from 'node:http';
      await assert.rejects(fetch('https://example.invalid'), /synthetic_external_network_denied/);
      const socket=new net.Socket(); assert.throws(()=>socket.connect(443,'203.0.113.1'),/synthetic_external_network_denied/); socket.destroy();
      assert.throws(()=>dns.lookup('example.invalid',()=>{}),/synthetic_external_network_denied/);
      const server=createServer((req,res)=>res.end('synthetic')); await new Promise(r=>server.listen(0,'127.0.0.1',r));
      assert.equal(await (await fetch('http://127.0.0.1:'+server.address().port)).text(),'synthetic');
      server.closeAllConnections(); await new Promise(r=>server.close(r));
    `], { env: { ...process.env, EVERNOOK_STABILITY_NETWORK_LOG: log }, timeout: 15000 });
  assert.deepEqual(readFileSync(log, 'utf8').trim().split('\n').map(value => JSON.parse(value).kind), ['fetch', 'tcp', 'dns']);
});
test('installed Python denies external TCP/DNS before connecting while socketpair remains usable', () => {
  const log = join(root, 'python.jsonl');
  execFileSync(process.env.EVERNOOK_TEST_PYTHON, ['-c', `
import socket
for action in (lambda: socket.create_connection(('203.0.113.1',443)), lambda: socket.getaddrinfo('example.invalid',443)):
    try: action()
    except OSError as error: assert str(error)=='synthetic_external_network_denied'
    else: raise AssertionError('network restriction missing')
a,b=socket.socketpair()
try:
    a.sendall(b'synthetic'); assert b.recv(9)==b'synthetic'
finally: a.close(); b.close()
`], { env: { ...process.env, EVERNOOK_STABILITY_NETWORK_LOG: log }, timeout: 15000 });
  assert.equal(readFileSync(log, 'utf8').trim().split('\n').length, 2);
});
test('Python guard uses a private journal selection when MCP-style environment omits custom variables', () => {
  const copy = join(root, 'sitecustomize.py'); const log = join(root, 'fallback.jsonl');
  writeFileSync(copy, readFileSync(resolve(import.meta.dirname, '../stability_network.py')));
  writeFileSync(join(root, 'stability-network-journal.txt'), log);
  execFileSync(process.env.EVERNOOK_TEST_PYTHON, ['-S', '-c', `
import runpy,socket,sys
runpy.run_path(sys.argv[1])
try: socket.getaddrinfo('example.invalid',443)
except OSError as error: assert str(error)=='synthetic_external_network_denied'
else: raise AssertionError('missing private journal restriction')
`, copy], { env: { ...process.env, EVERNOOK_STABILITY_NETWORK_LOG: undefined }, timeout: 15000 });
  assert.equal(JSON.parse(readFileSync(log, 'utf8')).kind, 'dns');
});
