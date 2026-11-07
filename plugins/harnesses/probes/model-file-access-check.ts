/** Small real-library regression: resolve a synthetic graph path without consuming its body. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { Socket } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';
import { withModelFileAccess } from './model-file-access.ts';

const [profilePath, output, mode] = process.argv.slice(2);
if (!profilePath || !output || !['original', 'verified'].includes(mode)) throw new Error('profile_output_mode_required');
const checkout = fs.realpathSync.native(resolve(import.meta.dirname, '../../..'));
const destination = resolve(output);
const inside = relative(checkout, join(fs.realpathSync.native(dirname(destination)), basename(destination)));
if (!inside || (inside !== '..' && !inside.startsWith('..' + sep) && !isAbsolute(inside))) throw new Error('external_output_required');
await mkdir(destination);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const profile = JSON.parse(await readFile(profilePath, 'utf8'));
const info = JSON.parse(await readFile(join(profile.transformers_dir, 'package.json'), 'utf8'));
if (info.version !== '4.2.0') throw new Error('model_dependency_mismatch');
const model = join(destination, 'model'); await mkdir(join(model, 'onnx'), { recursive: true });
const graph = join(model, 'onnx/model_q4.onnx'); const data = Buffer.alloc(8 * 1024 * 1024, 0x63);
await writeFile(graph, data);
assert.equal(createHash('sha256').update(await readFile(graph)).digest('hex'), createHash('sha256').update(data).digest('hex'));
const observations: { bytes: number; ended: boolean }[] = [];
const original = fs.createReadStream;
fs.createReadStream = function (...args: Parameters<typeof fs.createReadStream>) {
  const stream = original(...args);
  if (resolve(String(args[0])) === graph) {
    const row = { bytes: 0, ended: false }; observations.push(row);
    stream.on('end', () => { row.bytes = stream.bytesRead; row.ended = true; });
  }
  return stream;
};
try {
  const { env } = await import(pathToFileURL(join(profile.transformers_dir, 'src/env.js')).href);
  const { getModelFile } = await import(pathToFileURL(join(profile.transformers_dir, 'src/utils/hub.js')).href);
  env.allowRemoteModels = false; env.allowLocalModels = true; env.useBrowserCache = false; env.useFSCache = false;
  env.useCustomCache = false; env.customCache = null;
  const result = await withModelFileAccess<string>(env, model, mode === 'verified' ? 'verified-local-path-v1' : 'library-default',
    () => getModelFile(model, 'onnx/model_q4.onnx', true, { local_files_only: true }, true));
  assert.equal(fs.realpathSync.native(result.value), fs.realpathSync.native(graph));
  const deadline = performance.now() + 1000;
  await delay(50);
  while (observations.some(row => !row.ended) && performance.now() < deadline) await delay(10);
  const report = { mode, body_read_by_caller: false, graph_bytes: data.length, path_hits: result.pathHits,
    file_response_streams: observations.length, completed_read_bytes: observations.reduce((n, row) => n + row.bytes, 0),
    streams: observations, network_attempts: networkAttempts,
    source_sha256: createHash('sha256').update(await readFile(join(profile.transformers_dir, 'src/utils/hub/FileResponse.js'))).digest('hex') };
  await writeFile(join(destination, 'result.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  assert.equal(networkAttempts, 0); assert.equal(env.useCustomCache, false); assert.equal(env.customCache, null);
  assert.equal(observations.length, 0, 'path-only resolution must not construct a reader for the unused body');
} finally { fs.createReadStream = original; }
