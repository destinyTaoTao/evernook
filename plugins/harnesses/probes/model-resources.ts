/** Bounded diagnostic only: fixed captured synthetic request, never model admission. */
import { createHash } from 'node:crypto';
import { realpathSync } from 'node:fs';
import { readFile, mkdir, appendFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { Socket } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { LocalQwenChat } from './local-qwen.ts';

const [profileFile, requestFile, output, mode, allocator = 'default'] = process.argv.slice(2);
if (![profileFile, requestFile, output].every(Boolean) || !['reuse', 'rebuild'].includes(mode)
    || !['default', 'noarena'].includes(allocator)) {
  throw new Error('Use --expose-gc model-resources.ts profile request external-output reuse|rebuild [default|noarena]');
}
const checkout = resolve(import.meta.dirname, '../../..');
const destination = resolve(output);
// Require an existing external parent and compare physical paths, including Windows aliases/casing.
const physicalDestination = join(realpathSync.native(dirname(destination)), basename(destination));
const containment = relative(realpathSync.native(checkout), physicalDestination);
if (!containment || (containment !== '..' && !containment.startsWith('..' + sep) && !isAbsolute(containment))) {
  throw new Error('external_output_required');
}
await mkdir(destination);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const profile = JSON.parse(await readFile(profileFile, 'utf8'));
const request = JSON.parse(await readFile(requestFile, 'utf8'));
const require = createRequire(join(profile.transformers_dir, 'package.json'));
const ort = require('onnxruntime-node');
let forwards = 0, outputs = 0, outputBytes = 0, finalized = 0, finalizedBytes = 0;
const largest: Record<string, { bytes: number; dims: number[]; type: string }> = {};
const finalizedOutputs = new FinalizationRegistry<number>(bytes => { finalized++; finalizedBytes += bytes; });
const original = ort.InferenceSession.prototype.run;
const originalCreate = ort.InferenceSession.create;
ort.InferenceSession.create = async function (model: unknown, options: Record<string, unknown>) {
  if (allocator === 'noarena' && options.enableCpuMemArena !== undefined) throw new Error('diagnostic_unexpected_allocator');
  const selected = allocator === 'noarena' ? { ...options, enableCpuMemArena: false } : options;
  await appendFile(join(destination, 'session-options.jsonl'), JSON.stringify(selected) + '\n');
  return originalCreate.call(this, model, selected);
};
ort.InferenceSession.prototype.run = async function (...args: unknown[]) {
  const result = await original.apply(this, args);
  forwards++;
  for (const [name, tensor] of Object.entries(result) as [string, { dims: number[]; type: string; data: { byteLength: number } }][]) {
    const bytes = tensor.data.byteLength;
    outputs++; outputBytes += bytes;
    finalizedOutputs.register(tensor, bytes);
    if (!largest[name] || largest[name].bytes < bytes) largest[name] = { bytes, dims: [...tensor.dims], type: tensor.type };
  }
  return result;
};
const started = performance.now();
async function phase(name: string) {
  // GC is a diagnostic intervention, never a latency/admission sample or a release guarantee.
  globalThis.gc?.(); await delay(50); globalThis.gc?.(); await delay(50);
  const row = { phase: name, elapsed_ms: performance.now() - started, observed_at_unix_ms: Date.now(), memory: process.memoryUsage(),
    forwards, outputs, output_bytes: outputBytes, finalized_outputs: finalized, finalized_output_bytes: finalizedBytes };
  await appendFile(join(destination, 'phases.jsonl'), JSON.stringify(row) + '\n');
}
await writeFile(join(destination, 'manifest.json'), JSON.stringify({ mode, allocator, repetitions: 2, max_new_tokens: 512,
  context_window: 32768, declared_memory_budget: 4096, request_sha256: createHash('sha256').update(JSON.stringify(request)).digest('hex'),
  probe_sha256: createHash('sha256').update(await readFile(import.meta.filename)).digest('hex'),
  model_probe_sha256: createHash('sha256').update(await readFile(new URL('./local-qwen.ts', import.meta.url))).digest('hex'),
  scope: 'fixed captured provider request; ORT output wrappers observed without retaining tensors; GC cannot establish native tensor release; not native publication or admission',
  gc_enabled: typeof globalThis.gc === 'function', pid: process.pid }) + '\n');
let model: LocalQwenChat | undefined;
try {
  await phase('before_load');
  for (let index = 0; index < 2; index++) {
    if (!model) {
      model = await LocalQwenChat.create(profileFile);
      await appendFile(join(destination, 'loads.jsonl'), JSON.stringify(model.startup) + '\n');
      await phase('loaded_' + index);
    }
    let text = '';
    for await (const chunk of model.stream(request, 512)) text += chunk;
    await writeFile(join(destination, `run-${index}.json`), JSON.stringify({ text, run: model.last }) + '\n');
    await phase('completed_' + index);
    if (mode === 'rebuild') {
      await model.close(); model = undefined;
      await phase('disposed_' + index);
    }
  }
} finally {
  if (model) { await model.close(); model = undefined; }
  await phase('final_disposed');
  await writeFile(join(destination, 'tensors.json'), JSON.stringify({ forwards, outputs, output_bytes: outputBytes,
    finalized_outputs: finalized, finalized_output_bytes: finalizedBytes, largest_outputs: largest, network_attempts: networkAttempts }) + '\n');
  ort.InferenceSession.prototype.run = original;
  ort.InferenceSession.create = originalCreate;
}
