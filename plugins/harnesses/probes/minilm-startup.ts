// Standalone cold-model diagnostic, separate from the measured public Core calls.
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { Socket } from 'node:net';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [profilePath, output] = process.argv.slice(2);
const profileText = await readFile(profilePath, 'utf8');
const profile = JSON.parse(profileText);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const files = ['config.json', 'onnx/model_quantized.onnx', 'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'unigram.json'];
const artifacts: Record<string, string> = {};
const before = performance.now();
for (const name of files) artifacts[name] = createHash('sha256').update(await readFile(join(profile.model_dir, name))).digest('hex');
const integrityMs = performance.now() - before;
const require = createRequire(pathToFileURL(join(profile.transformers_dir, 'package.json')));
if (JSON.parse(await readFile(join(profile.transformers_dir, 'package.json'), 'utf8')).version !== '4.2.0'
    || JSON.parse(await readFile(require.resolve('onnxruntime-node/package.json'), 'utf8')).version !== '1.24.3') throw new Error('dependency_mismatch');
const imported = performance.now();
const { env, pipeline } = await import(pathToFileURL(join(profile.transformers_dir, 'dist/transformers.node.mjs')).href);
const importMs = performance.now() - imported;
env.allowRemoteModels = false; env.allowLocalModels = true; env.useBrowserCache = false; env.useFSCache = false;
const loaded = performance.now();
const extractor = await pipeline('feature-extraction', profile.model_dir, {
  local_files_only: true, dtype: 'q8', device: 'cpu', session_options: { intraOpNumThreads: 2, interOpNumThreads: 1 },
});
const loadMs = performance.now() - loaded;
try {
  const started = performance.now();
  const result = await extractor(['业余时间我通常怎么放松？'], { pooling: 'mean', normalize: true });
  await writeFile(output, JSON.stringify({ profile_sha256: createHash('sha256').update(profileText).digest('hex'), artifacts,
    integrity_hash_ms: integrityMs, import_ms: importMs, model_load_ms: loadMs, inference_ms: performance.now() - started,
    dimensions: result.dims, network_attempts: networkAttempts,
    scope: 'Independent startup diagnostic using the same pipeline options. Artifact hashes are reported for comparison, not a substitute for public LocalMiniLM integrity validation.' }) + '\n', { flag: 'wx' });
} finally { await extractor.dispose(); }
