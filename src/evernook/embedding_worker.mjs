// Bounded offline inference, optionally reused by an owned Python session.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { Socket } from 'node:net';

let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); };
console.log = console.warn = console.error = () => {};
const session = process.argv[2] === '--session';
async function* requests() {
  let buffered = Buffer.alloc(0);
  for await (const chunk of process.stdin) {
    buffered = Buffer.concat([buffered, chunk]);
    if (buffered.length > 131072) throw new Error('size');
    if (session) {
      let end;
      while ((end = buffered.indexOf(10)) !== -1) {
        const line = buffered.subarray(0, end);
        buffered = buffered.subarray(end + 1);
        yield JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
      }
    }
  }
  if (!session) yield JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffered));
  else if (buffered.length) throw new Error('incomplete');
}

let extractor;
try {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major !== 24 || minor < 11) throw new Error('version');
  let binding;
  let lastId = 0;
  for await (const input of requests()) {
    if (!Array.isArray(input.texts) || input.texts.length < 1 || input.texts.length > 16
        || input.texts.some(text => typeof text !== 'string' || !text || Array.from(text).length > 256)) throw new Error('input');
    if (session && (!Number.isSafeInteger(input.id) || input.id !== lastId + 1)) throw new Error('id');
    const currentBinding = JSON.stringify([input.model_dir, input.transformers_dir]);
    if (binding !== undefined && currentBinding !== binding) throw new Error('binding');
    if (extractor === undefined) {
      const packageInfo = JSON.parse(readFileSync(join(input.transformers_dir, 'package.json'), 'utf8'));
      if (packageInfo.name !== '@huggingface/transformers' || packageInfo.version !== '4.2.0') throw new Error('version');
      const require = createRequire(pathToFileURL(join(input.transformers_dir, 'package.json')));
      if (JSON.parse(readFileSync(require.resolve('onnxruntime-node/package.json'), 'utf8')).version !== '1.24.3') throw new Error('version');
      const { env, pipeline } = await import(pathToFileURL(join(input.transformers_dir, 'dist/transformers.node.mjs')).href);
      env.allowRemoteModels = false;
      env.allowLocalModels = true;
      env.useBrowserCache = false;
      env.useFSCache = false;
      // Python has verified every pinned asset for this call. In Transformers 4.2,
      // FileResponse eagerly streams even an ONNX file requested only by path.
      // Resolve just this graph through the read-only cache; ORT still reads it.
      const graph = 'onnx/model_quantized.onnx';
      const aliases = new Set([
        input.model_dir.replace(/\/$/, '') + '/' + graph,
        env.remoteHost.replace(/\/$/, '') + '/' + env.remotePathTemplate
          .replaceAll('{model}', input.model_dir).replaceAll('{revision}', 'main')
          .replace(/^\//, '').replace(/\/$/, '') + '/' + graph,
      ]);
      const oldEnabled = env.useCustomCache;
      const oldCache = env.customCache;
      try {
        env.useCustomCache = true;
        env.customCache = {
          async match(request) { return aliases.has(request) ? join(input.model_dir, graph) : undefined; },
          async put() { throw new Error('read_only_model_path'); },
        };
        extractor = await pipeline('feature-extraction', input.model_dir, {
          local_files_only: true, dtype: 'q8', device: 'cpu',
          session_options: { intraOpNumThreads: 2, interOpNumThreads: 1 },
        });
      } finally {
        env.useCustomCache = oldEnabled;
        env.customCache = oldCache;
      }
    binding = currentBinding;
    }
    // The upstream pipeline truncates by default; reject rather than silently omit evidence.
    // Per-item token overflow yields a null vector so one oversize piece cannot wedge a batch.
    const embeddable = input.texts.map(text =>
      extractor.tokenizer(text, { truncation: false, padding: false }).input_ids.dims.at(-1) <= 512);
    const selected = input.texts.filter((_, index) => embeddable[index]);
    let valid = [];
    if (selected.length) {
      const output = await extractor(selected, { pooling: 'mean', normalize: true });
      try { valid = output.tolist(); }
      finally { output.dispose(); }
    }
    const vectors = [];
    let cursor = 0;
    for (let index = 0; index < input.texts.length; index += 1) {
      vectors.push(embeddable[index] ? valid[cursor] : null);
      if (embeddable[index]) cursor += 1;
    }
    if (networkAttempts) throw new Error('network');
    const result = { vectors, network_attempts: networkAttempts };
    if (session) result.id = input.id;
    process.stdout.write(JSON.stringify(result) + '\n');
    lastId = input.id;
  }
} catch {
  process.exitCode = 1;
} finally {
  if (extractor !== undefined) await extractor.dispose();
}
