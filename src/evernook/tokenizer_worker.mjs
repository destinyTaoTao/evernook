// Local, integrity-pinned tokenization only. Never echoes text or paths on failure.
import { readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { Socket } from 'node:net';

globalThis.fetch = async () => { throw new Error('offline'); };
Socket.prototype.connect = function () { throw new Error('offline'); };
console.log = console.warn = console.error = () => {};
let tokenizer;
let configuration;
async function respond(bytes) {
  const input = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  if (typeof input.text !== 'string' || input.text.length > 2 * 1048576) throw new Error('input');
  if (input.id !== undefined && (!Number.isSafeInteger(input.id) || input.id < 1)) throw new Error('id');
  const identity = JSON.stringify([input.package_dir, input.tokenizer_file, input.config_file,
    input.tokenizer_sha256, input.config_sha256]);
  if (configuration !== undefined && configuration !== identity) throw new Error('configuration');
  const info = JSON.parse(readFileSync(join(input.package_dir, 'package.json'), 'utf8'));
  if (info.name !== '@huggingface/tokenizers' || info.version !== '0.2.0') throw new Error('version');
  const load = (path, hash) => {
    if (statSync(path).size > 64 * 1024 * 1024) throw new Error('size');
    const bytes = readFileSync(path);
    if (createHash('sha256').update(bytes).digest('hex') !== hash) throw new Error('integrity');
    return bytes;
  };
  // Hash every request, even when the already-verified tokenizer is reused.
  const tokenizerBytes = load(input.tokenizer_file, input.tokenizer_sha256);
  const configBytes = load(input.config_file, input.config_sha256);
  if (tokenizer === undefined) {
    const { Tokenizer } = await import(pathToFileURL(join(input.package_dir, 'dist/tokenizers.mjs')).href);
    const decode = bytes => JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    tokenizer = new Tokenizer(decode(tokenizerBytes), decode(configBytes));
    configuration = identity;
  }
  const count = tokenizer.encode(input.text, { add_special_tokens: false }).ids.length;
  process.stdout.write(JSON.stringify(input.id === undefined ? { count } : { id: input.id, count }) + '\n');
}
try {
  let chunks = [];
  let size = 0;
  const append = chunk => {
    size += chunk.length;
    if (size > 8 * 1024 * 1024) throw new Error('size');
    chunks.push(chunk);
  };
  for await (const chunk of process.stdin) {
    let start = 0;
    for (let end = chunk.indexOf(10, start); end !== -1; end = chunk.indexOf(10, start)) {
      append(chunk.subarray(start, end));
      await respond(Buffer.concat(chunks));
      chunks = [];
      size = 0;
      start = end + 1;
    }
    if (start < chunk.length) append(chunk.subarray(start));
  }
  // Preserve the existing one-shot EOF protocol for standalone count callers.
  if (size) await respond(Buffer.concat(chunks));
} catch {
  process.exitCode = 1;
}
