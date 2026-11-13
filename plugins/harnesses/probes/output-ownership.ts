/** A 116-byte synthetic Add graph checks the actual pinned CPU output boundary, not model admission. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createReadStream, realpathSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { Socket } from 'node:net';

const [profileFile, output, expected] = process.argv.slice(2);
if (!profileFile || !output || !['copy', 'reuse'].includes(expected)) throw new Error('Use output-ownership.ts PROFILE NEW_EXTERNAL copy|reuse');
if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('ownership_probe_environment_unsupported');
const target = resolve(output);
const checkout = realpathSync.native(resolve(import.meta.dirname, '../../..'));
const inside = relative(checkout, join(realpathSync.native(dirname(target)), basename(target)));
if (!inside || (inside !== '..' && !inside.startsWith('..' + sep) && !isAbsolute(inside))) throw new Error('external_output_required');
await mkdir(target);
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const profile = JSON.parse(await readFile(profileFile, 'utf8'));
const require = createRequire(join(profile.transformers_dir, 'package.json'));
const infoFile = require.resolve('onnxruntime-node/package.json');
const info = JSON.parse(await readFile(infoFile, 'utf8'));
assert.equal(info.version, '1.24.3');
async function digest(path: string) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}
const binaryRoot = join(dirname(infoFile), 'bin/napi-v6/win32/x64');
const identities = { binding_sha256: await digest(join(binaryRoot, 'onnxruntime_binding.node')),
  runtime_dll_sha256: await digest(join(binaryRoot, 'onnxruntime.dll')) };
// Generated with onnx.helper: y = Add(x, [1,1,1,1]), float32[4], opset17/IR8. No pretrained data.
const graph = Buffer.from('CAg6agoQCgF4CgNvbmUSAXkiA0FkZBIXc3ludGhldGljLXByZWFsbG9jYXRpb24qGwgEEAEiEAAAgD8AAIA/AACAPwAAgD9CA29uZVoPCgF4EgoKCAgBEgQKAggEYg8KAXkSCgoICAESBAoCCARCBAoAEBE=', 'base64');
const graphHash = createHash('sha256').update(graph).digest('hex');
assert.equal(graphHash, '0d496258c98ed83808e440e2381474e648f4de664a78bfd69e366e9bb088a19d');
await writeFile(join(target, 'synthetic-add.onnx'), graph);
const ort = require('onnxruntime-node');
const session = await ort.InferenceSession.create(join(target, 'synthetic-add.onnx'), { executionProviders: ['cpu'], intraOpNumThreads: 1 });
let returned: Float32Array | undefined;
let sameBuffer = false, sameTensor = false;
try {
  const input = new ort.Tensor('float32', new Float32Array([1, 2, 3, 4]), [4]);
  const backing = new Float32Array(6).fill(-13);
  const data = new Float32Array(backing.buffer, 4, 4);
  const outputTensor = new ort.Tensor('float32', data, [4]);
  const result = await session.run({ x: input }, { y: outputTensor });
  assert.ok(result.y.data instanceof Float32Array); returned = result.y.data;
  assert.deepEqual([...data], [2, 3, 4, 5]); assert.deepEqual([...returned!], [2, 3, 4, 5]);
  assert.equal(backing[0], -13); assert.equal(backing[5], -13);
  sameBuffer = data.buffer === returned!.buffer; sameTensor = result.y === outputTensor;
  data[0] = 99; assert.equal(returned![0], sameBuffer ? 99 : 2);
  await assert.rejects(session.run({ x: input }, { y: new ort.Tensor('float32', new Float32Array(5), [5]) }), /shape|dimension/i);
} finally { await session.release(); }
assert.ok(returned); globalThis.gc?.();
assert.deepEqual([...returned], [sameBuffer ? 99 : 2, 3, 4, 5]);
await writeFile(join(target, 'report.json'), JSON.stringify({ version: info.version, ...identities,
  expected, graph_sha256: graphHash, caller_buffer_filled: true, boundary_sentinels_unchanged: true,
  returned_buffer_is_caller_buffer: sameBuffer, returned_tensor_is_caller_tensor: sameTensor,
  wrong_shape_rejected: true, output_readable_after_session_release: true, network_attempts: networkAttempts,
  scope: 'Synthetic CPU numeric preallocated-output ownership only; no Qwen/model/admission verdict' }, null, 2) + '\n');
assert.equal(networkAttempts, 0);
assert.equal(sameBuffer, expected === 'reuse', 'cpu_output_ownership_mismatch');
