/** Separately pinned, same-machine experimental serialization; no model admission. */
import { createHash } from 'node:crypto';
import { createReadStream, realpathSync } from 'node:fs';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { cpus } from 'node:os';
import { Socket } from 'node:net';
import { artifacts, checkExternalPackedEnvironment, externalPackedArtifacts, externalPackedProfile, lastHiddenGraph } from './local-qwen.ts';

const [profileFile, output] = process.argv.slice(2);
if (!profileFile || !output) throw new Error('Use export-external-qwen.ts LAST_HIDDEN_PROFILE NEW_EXTERNAL_ROOT');
checkExternalPackedEnvironment();
const destination = resolve(output);
const checkout = realpathSync.native(resolve(import.meta.dirname, '../../..'));
const target = join(realpathSync.native(dirname(destination)), basename(destination));
const inside = relative(checkout, target);
if (!inside || (inside !== '..' && !inside.startsWith('..' + sep) && !isAbsolute(inside))) throw new Error('external_output_required');
const profile = JSON.parse(await readFile(profileFile, 'utf8'));
if (profile.profile !== 'qwen3-0.6b-last-hidden-experimental-v1') throw new Error('export_identity_mismatch');
const source = realpathSync.native(profile.model_dir);
const overlap = (parent: string, child: string) => {
  const rel = relative(parent, child);
  return !rel || (rel !== '..' && !rel.startsWith('..' + sep) && !isAbsolute(rel));
};
if (overlap(source, target) || overlap(target, source)) throw new Error('export_must_not_overlap_source');
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
async function digest(path: string) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}
const expected = { ...artifacts, 'onnx/model_q4.onnx': lastHiddenGraph };
for (const [name, hash] of Object.entries(expected)) {
  if (await digest(join(source, name)) !== hash) throw new Error('source_model_integrity_mismatch');
}
const require = createRequire(join(profile.transformers_dir, 'package.json'));
if (require('onnxruntime-node/package.json').version !== '1.24.3'
    || JSON.parse(await readFile(join(profile.transformers_dir, 'package.json'), 'utf8')).version !== '4.2.0') {
  throw new Error('model_dependency_mismatch');
}
const ort = require('onnxruntime-node');
await mkdir(destination); // Refuse replacement of every previous candidate, including a partial export.
const modelDir = join(destination, 'model'); await mkdir(join(modelDir, 'onnx'), { recursive: true });
const options = { intraOpNumThreads: 4, interOpNumThreads: 1, enableCpuMemArena: false,
  optimizedModelFilePath: join(modelDir, 'onnx/model_q4.onnx'), extra: { session: {
    optimized_model_external_initializers_file_name: 'weights.bin',
    optimized_model_external_initializers_min_size_in_bytes: '1024',
    save_external_prepacked_constant_initializers: '1' } } };
const session = await ort.InferenceSession.create(join(source, 'onnx/model_q4.onnx'), options);
try {
  for (const name of Object.keys(artifacts).filter(name => !name.startsWith('onnx/'))) await copyFile(join(source, name), join(modelDir, name));
  const files: Record<string, string> = {};
  for (const [name, hash] of Object.entries({ ...artifacts, ...externalPackedArtifacts })) {
    files[name] = await digest(join(modelDir, name));
    if (files[name] !== hash) throw new Error('exported_model_integrity_mismatch');
  }
  await writeFile(join(destination, 'manifest.json'), JSON.stringify({ candidate: externalPackedProfile,
    parent_model_sha256: lastHiddenGraph, artifacts: files, options, cpu: cpus()[0].model,
    platform: process.platform, arch: process.arch, onnxruntime: '1.24.3', transformers: '4.2.0',
    exporter_sha256: await digest(import.meta.filename), input_names: session.inputNames, output_names: session.outputNames,
    network_attempts: networkAttempts, admission: 'blocked_pending_independent_validation' }, null, 2) + '\n');
  await writeFile(join(destination, 'profile.json'), JSON.stringify({ ...profile, profile: externalPackedProfile,
    model_dir: modelDir, file_access: 'verified-local-path-v1' }, null, 2) + '\n');
} finally { await session.release(); }
