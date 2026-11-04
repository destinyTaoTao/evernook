/** Isolate one real frozen-model prefill. No answer generation or admission claim. */
import { createHash } from 'node:crypto';
import { realpathSync } from 'node:fs';
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { Socket } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { LocalQwenChat } from './local-qwen.ts';

const [profileFile, requestFile, output, variant] = process.argv.slice(2);
if (![profileFile, requestFile, output].every(Boolean)
    || !['baseline', 'no-prepack', 'no-logits-fetch'].includes(variant) || typeof globalThis.gc !== 'function') {
  throw new Error('Use node --expose-gc prefill-resources.ts profile request external-output baseline|no-prepack|no-logits-fetch');
}
const destination = resolve(output);
const checkout = resolve(import.meta.dirname, '../../..');
const inside = relative(realpathSync.native(checkout), join(realpathSync.native(dirname(destination)), basename(destination)));
if (!inside || (inside !== '..' && !inside.startsWith('..' + sep) && !isAbsolute(inside))) throw new Error('external_output_required');
await mkdir(destination);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const profile = JSON.parse(await readFile(profileFile, 'utf8'));
const request = JSON.parse(await readFile(requestFile, 'utf8'));
const require = createRequire(join(profile.transformers_dir, 'package.json'));
const ort = require('onnxruntime-node');
type Tensor = { type: string; dims: number[]; data: ArrayBufferView & { length: number; [key: number]: number | bigint } };
type Session = { inputNames: string[]; outputNames: string[]; inputMetadata: unknown; outputMetadata: unknown; endProfiling(): void };
let session: Session | undefined;
let feeds: Record<string, Tensor> | undefined;
const captureOnly = new Error('diagnostic_feeds_captured_before_inference');
const originalCreate = ort.InferenceSession.create;
const originalRun = ort.InferenceSession.prototype.run;
ort.InferenceSession.create = async function (model: unknown, options: Record<string, unknown>) {
  const selected = { ...options, enableProfiling: true,
    profileFilePrefix: join(destination, 'ort'),
    ...(variant === 'no-prepack' ? { extra: { session: { disable_prepacking: '1' } } } : {}) };
  if (variant === 'no-prepack' && options.extra !== undefined) throw new Error('diagnostic_unexpected_session_extras');
  await writeFile(join(destination, 'session-options.json'), JSON.stringify(selected) + '\n');
  return originalCreate.call(this, model, selected);
};
ort.InferenceSession.prototype.run = async function (input: Record<string, Tensor>) {
  if (session) throw new Error('diagnostic_multiple_sessions');
  session = this; feeds = input;
  throw captureOnly;
};
const hash = (view: ArrayBufferView) => createHash('sha256')
  .update(new Uint8Array(view.buffer, view.byteOffset, view.byteLength)).digest('hex');
async function phase(name: string, minimumOutputBytes = 0) {
  globalThis.gc!(); await delay(50); globalThis.gc!(); await delay(50);
  const row = { phase: name, observed_at_unix_ms: Date.now(), memory: process.memoryUsage(),
    minimum_output_bytes: minimumOutputBytes };
  await appendFile(join(destination, 'phases.jsonl'), JSON.stringify(row) + '\n');
  if (row.memory.arrayBuffers < minimumOutputBytes) throw new Error('diagnostic_output_ownership_lost');
  await delay(750); // At least two 250ms process samples while this ownership state is stable.
}
await writeFile(join(destination, 'manifest.json'), JSON.stringify({ variant, pid: process.pid,
  scope: 'one direct ORT prefill with actual SDK-captured synthetic request feeds; no answer/durable publication or quality verdict',
  request_sha256: createHash('sha256').update(JSON.stringify(request)).digest('hex'),
  probe_sha256: createHash('sha256').update(await readFile(import.meta.filename)).digest('hex'),
  model_probe_sha256: createHash('sha256').update(await readFile(new URL('./local-qwen.ts', import.meta.url))).digest('hex'),
  original_generation_allowance: 512, original_context_window: 32768 }) + '\n');
let model: LocalQwenChat | undefined;
try {
  await phase('before_load');
  model = await LocalQwenChat.create(profileFile);
  await writeFile(join(destination, 'load.json'), JSON.stringify(model.startup) + '\n');
  await phase('model_loaded');
  try { for await (const _chunk of model.stream(request, 512)) throw new Error('diagnostic_unexpected_generation'); }
  catch (error) { if (error !== captureOnly) throw error; }
  if (!session || !feeds) throw new Error('diagnostic_feeds_not_captured');
  const inputMetadata = Object.fromEntries(Object.entries(feeds).map(([name, tensor]) =>
    [name, { type: tensor.type, dims: tensor.dims, bytes: tensor.data.byteLength, sha256: hash(tensor.data) }]));
  await writeFile(join(destination, 'graph.json'), JSON.stringify({ input_names: session.inputNames,
    output_names: session.outputNames, input_metadata: session.inputMetadata, output_metadata: session.outputMetadata,
    input_tokens: model.last?.input_tokens, feeds: inputMetadata }) + '\n');
  await phase('feeds_ready');
  const started = performance.now();
  let result: Record<string, Tensor> | undefined = await originalRun.call(session, feeds,
    variant === 'no-logits-fetch' ? session.outputNames.filter(name => name !== 'logits') : session.outputNames);
  const forwardMs = performance.now() - started;
  const metadata = Object.fromEntries(Object.entries(result!).map(([name, tensor]) =>
    [name, { type: tensor.type, dims: tensor.dims, bytes: tensor.data.byteLength, sha256: hash(tensor.data) }]));
  let firstGreedyToken: number | null = null;
  if (result!.logits) {
    const logits = result!.logits;
    const vocabulary = logits.dims.at(-1)!;
    const start = logits.data.length - vocabulary;
    let best = 0;
    for (let index = 1; index < vocabulary; index++) if (logits.data[start + index] > logits.data[start + best]) best = index;
    firstGreedyToken = best;
  }
  await writeFile(join(destination, 'forward.json'), JSON.stringify({ forward_ms: forwardMs,
    first_greedy_token: firstGreedyToken, outputs: metadata }) + '\n');
  const outputRoot = Symbol('evernook_prefill_outputs');
  Object.defineProperty(globalThis, outputRoot, { value: result, configurable: true });
  result = undefined;
  try { await phase('outputs_held', Object.values(metadata).reduce((total, tensor) => total + tensor.bytes, 0)); }
  finally { Reflect.deleteProperty(globalThis, outputRoot); }
  await phase('outputs_released');
  feeds = undefined;
  session.endProfiling(); session = undefined;
  await model.close(); model = undefined;
  await phase('session_released');
} finally {
  ort.InferenceSession.create = originalCreate;
  ort.InferenceSession.prototype.run = originalRun;
  if (model) await model.close();
  await writeFile(join(destination, 'network.json'), JSON.stringify({ network_attempts: networkAttempts }) + '\n');
}
