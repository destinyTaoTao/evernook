import assert from 'node:assert/strict';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { mkdir, mkdtemp, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { TranscriptionClient as SourceClient, type TranscriptionMaterial } from '../src/transcription.ts';

const TranscriptionClient: typeof SourceClient = process.env.EVERNOOK_TEST_TRANSCRIPTION_MODULE
  ? (await import(pathToFileURL(process.env.EVERNOOK_TEST_TRANSCRIPTION_MODULE).href)).TranscriptionClient
  : SourceClient;

const python = process.env.EVERNOOK_TEST_PYTHON;
const output = process.env.EVERNOOK_TEST_OUTPUT;
if (!python || !output) throw new Error('Set EVERNOOK_TEST_PYTHON and EVERNOOK_TEST_OUTPUT');
const checkout = resolve(import.meta.dirname, '../../..');

test('trusted SDK routes transcription to isolated service with privacy and withdrawal', async () => {
  await mkdir(output, { recursive: true });
  const root = await mkdtemp(join(output, 'transcription-sdk-'));
  const script = "import json,sys; from pathlib import Path; from evernook import initialize,pair_client,MemoryRuntime; from evernook.protected import initialize_protected_inbox; r=Path(sys.argv[1]); c,a=initialize(r/'ordinary',client_id='owner',entry='owner'); p=c.parent/'mcp-access-owner.json'; pair_client(c,client_id='owner',entry='owner',access_path=p); initialize_protected_inbox(MemoryRuntime(c),r/'inbox',r/'keys',b'synthetic test password'); print(json.dumps({'config':str(c),'access':str(p)}))";
  const paths = JSON.parse(execFileSync(python, ['-c', script, root], { cwd: checkout, encoding: 'utf8' })) as { config: string; access: string };
  const client = new TranscriptionClient({ python, ...paths, cwd: checkout });
  const base: TranscriptionMaterial = { source: { entry: 'owner', session: 'synthetic', branch: 'voice', event: '1' },
    sequence: 1, occurred_at: null, completion: 'complete', text: 'synthetic ordinary transcript',
    provider: 'synthetic', model: 'model-v1', language: 'en-US', execution: 'local', start_ms: 0, end_ms: 500 };
  const privateText = 'synthetic-private-transcript-sentinel-183';
  const secret: TranscriptionMaterial = { ...base, source: { ...base.source, event: '2' }, sequence: 2,
    text: privateText, sensitive: true };
  let service: ChildProcess | undefined;
  const stop = async () => {
    if (!service || service.exitCode !== null || service.signalCode !== null) return;
    const ended = new Promise<void>(done => service!.once('exit', () => done()));
    service.kill(); await ended;
  };
  const start = async (vault: boolean) => {
    const endpoint = join(root, 'ordinary', 'memory-service.json');
    const old = await readFile(endpoint).catch(() => undefined);
    service = spawn(python, ['-B', '-m', 'evernook.service', '--config', paths.config,
      '--activate-owner', paths.access, ...(vault ? ['--protected-inbox', join(root, 'inbox'), '--protected-keys', join(root, 'keys')] : [])],
    { cwd: checkout, windowsHide: true, stdio: 'ignore' });
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && service.exitCode === null) {
      const current = await readFile(endpoint).catch(() => undefined);
      if (current && (!old || !current.equals(old))) return;
      await new Promise(done => setTimeout(done, 50));
    }
    throw new Error('isolated service readiness failed');
  };
  try {
    await start(false);
    await assert.rejects(client.submit(secret), /evernook:protected_service_unconfigured/);
    await stop();
    await start(true);
    if (process.env.EVERNOOK_TEST_NO_CRYPTO_PYTHON) {
      const noCrypto = new TranscriptionClient({ python: process.env.EVERNOOK_TEST_NO_CRYPTO_PYTHON, ...paths, cwd: checkout });
      await assert.rejects(noCrypto.submit(secret), /evernook:vault_dependency_missing/);
    }
    const first = await client.submit(base);
    assert.deepEqual(await client.submit(base), first);
    assert.equal(first.storage, 'ordinary');
    const receipt = first.receipt as { message: { message_id: string } };
    const read = await client.read(receipt.message.message_id);
    assert.equal((read?.transcription as { confirmation: string }).confirmation, 'unverified');
    const protectedResult = await client.submit(secret);
    assert.equal(protectedResult.storage, 'protected');
    assert(!JSON.stringify(protectedResult).includes(privateText));
    const protectedReceipt = protectedResult.receipt as { message: { message_id: string } };
    assert.equal(await client.read(protectedReceipt.message.message_id), null);
    const canceled = AbortSignal.abort();
    await assert.rejects(client.submit({ ...base, source: { ...base.source, event: '3' } }, canceled), /operation_canceled/);
    const invoke = (command: string, value: object) => JSON.parse(execFileSync(python, ['-m', 'evernook.cli', command,
      '--config', paths.config, '--access', paths.access, '--require-service'],
    { cwd: checkout, input: JSON.stringify(value), encoding: 'utf8' })) as { result: { message_id: string } };
    const control = invoke('remember', { source: { entry: 'owner', session: 'synthetic', branch: 'voice', event: '4' },
      role: 'user', sequence: 4, occurred_at: null, completion: 'complete', text: 'synthetic hide prior transcript' });
    invoke('change', { request_id: 'hide-transcript', target_id: receipt.message.message_id, expected_version: 1,
      action: 'suppress', evidence_id: control.result.message_id, reason: 'synthetic' });
    assert.equal(await client.read(receipt.message.message_id), null);
  } finally { await stop(); }
  await assert.rejects(client.submit(secret), /evernook:/);
  assert(!(await readFile(join(root, 'ordinary', 'memory.sqlite3'))).includes(Buffer.from(privateText)));
});
