// Local, integrity-pinned tokenization only. Never echoes text or paths on failure.
import { readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { Socket } from 'node:net';

globalThis.fetch = async () => { throw new Error('offline'); };
Socket.prototype.connect = function () { throw new Error('offline'); };
console.log = console.warn = console.error = () => {};
try {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 8 * 1024 * 1024) throw new Error('size');
    chunks.push(chunk);
  }
  const input = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
  if (typeof input.text !== 'string' || input.text.length > 2 * 1048576) throw new Error('input');
  const info = JSON.parse(readFileSync(join(input.package_dir, 'package.json'), 'utf8'));
  if (info.name !== '@huggingface/tokenizers' || info.version !== '0.2.0') throw new Error('version');
  const load = (path, hash) => {
    if (statSync(path).size > 64 * 1024 * 1024) throw new Error('size');
    const bytes = readFileSync(path);
    if (createHash('sha256').update(bytes).digest('hex') !== hash) throw new Error('integrity');
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  };
  const { Tokenizer } = await import(pathToFileURL(join(input.package_dir, 'dist/tokenizers.mjs')).href);
  const tokenizer = new Tokenizer(load(input.tokenizer_file, input.tokenizer_sha256), load(input.config_file, input.config_sha256));
  const count = tokenizer.encode(input.text, { add_special_tokens: false }).ids.length;
  process.stdout.write(JSON.stringify({ count }) + '\n');
} catch {
  process.exitCode = 1;
}
