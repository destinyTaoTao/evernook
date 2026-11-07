// Drive only the explicit disposable native-tools-plugin bundle, never a user profile.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, isAbsolute, sep } from 'node:path';
import { MemoryClient } from '../../harnesses/dist/client.js';

async function main() {
  const [logPath, reportPath, action = 'inventory', pathsPath] = process.argv.slice(2);
  const launch = readFileSync(logPath, 'utf8').match(/http:\/\/127\.0\.0\.1:\d+\/\?\S+/)?.[0];
  assert.ok(launch, 'Missing synthetic loopback launch URL');
  const base = new URL(launch).origin;
  const initial = await fetch(launch, { redirect: 'manual' });
  const cookie = initial.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
  assert.ok(cookie);
  const response = await fetch(base + '/api/evernook-synthetic94/probe', { method: 'POST',
    headers: { Origin: base, Cookie: cookie, 'Content-Type': 'application/json' },
    body: JSON.stringify(action === 'inventory' ? { action } : { case: action }),
  });
  const body = await response.text();
  const result = body ? JSON.parse(body) : { synthetic: true, error: 'empty native host response' };
  writeFileSync(reportPath, JSON.stringify(result, null, 2));
  assert.equal(response.status, 200);
  assert.equal(result.synthetic, true);
  if (action === 'inventory') {
    console.log(JSON.stringify({ synthetic: true, tools: result.tools.map(value => value.name) })); return;
  }
  console.log(JSON.stringify({ case: result.case, requests: result.requests, timed_out: result.timed_out,
    results: result.results?.length, blocked: result.blocked?.length, errors: result.errors?.length }));
  assert.equal(result.timed_out, false);
  assert.equal(result.requests, result.expected_requests);
  assert.equal(result.blocked.length, 0);
  assert.equal(result.errors.length, 0);
  assert.ok(result.results.length >= 1);
  const rootResult = result.results.find(value => value.type === 'tool/result');
  assert.ok(rootResult);
  assert.equal(Boolean(rootResult.data.message.isError), result.expected_error);
  if (result.expected_text) assert.ok(JSON.stringify(result.results).includes(result.expected_text), 'Expected synthetic output is missing');
  if (result.expected_file) {
    const file = resolve(result.workspace, result.expected_file.path);
    const within = relative(result.workspace, file);
    assert.ok(within && !within.startsWith('..' + sep) && !isAbsolute(within));
    assert.equal(readFileSync(file, 'utf8'), result.expected_file.content);
  }
  assert.ok(pathsPath && process.env.EVERNOOK_TEST_PYTHON);
  const paths = JSON.parse(readFileSync(pathsPath, 'utf8'));
  const reader = new MemoryClient({ python: process.env.EVERNOOK_TEST_PYTHON, config: paths.config, access: paths.access, entry: 'dsh' });
  try {
    await reader.connect();
    const found = await reader.call('search', { query: 'evernook_tool_publication_v1', mode: 'exact', limit: 100 });
    const snapshots = [];
    for (const hit of found.hits.filter(value => value.source.session === result.session && value.role === 'tool')) {
      snapshots.push(JSON.parse((await reader.read(hit.message_id)).event.text));
    }
    for (const event of result.results) {
      if (event.type === 'tool/result') {
        const snapshot = snapshots.find(value => value.phase === 'result' && value.call_id === event.data.message.toolCallId);
        assert.ok(snapshot, 'A visible native result has no prior ledger snapshot');
        assert.deepEqual(event.data.message.content, snapshot.result.content);
      } else {
        const snapshot = snapshots.find(value => value.phase === 'ptc-log' && value.call_id === event.data.subCallId);
        assert.ok(snapshot, 'A visible PTC log has no prior ledger snapshot');
        assert.deepEqual(event.data, snapshot.result.value);
      }
    }
    for (const event of result.calls ?? []) {
      const snapshot = snapshots.find(value => value.call_id === (event.data.subCallId ?? event.data.callId)
        && value.phase === (event.type === 'tool/call' ? 'call' : 'ptc-call'));
      assert.ok(snapshot, 'A visible call has no ledger snapshot');
      if (event.type === 'tool/call') assert.deepEqual(JSON.parse(event.data.arguments), snapshot.result.arguments);
      else assert.deepEqual(event.data, snapshot.result.value);
    }
    result.ledger_snapshots = snapshots.length;
    result.exact_native_results_checked = true;
    writeFileSync(reportPath, JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ exact_native_results_checked: true, ledger_snapshots: snapshots.length }));
  } finally { await reader.close(); }
}
await main().catch(error => { console.error(error.message); process.exitCode = 1; });
