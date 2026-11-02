import { createHash } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import { Tokenizer } from '@huggingface/tokenizers';

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function json(path: string): Record<string, unknown> {
  if (!isAbsolute(path) || statSync(path).size > 64 * 1024 * 1024) throw new Error('evernook:invalid_tokenizer_config');
  const value: unknown = JSON.parse(readFileSync(path, 'utf8'));
  if (!object(value)) throw new Error('evernook:invalid_tokenizer_config');
  return value;
}

/** Independently count the exact payload supplied by Python, using pinned local assets. */
export function loadTargetCounter(contextPath: string) {
  try {
    const context = json(contextPath);
    if (context.schema_version !== 1 || typeof context.tokenizer_config !== 'string') throw new Error();
    const config = json(context.tokenizer_config);
    if (config.schema_version !== 1 || typeof config.model_id !== 'string') throw new Error();
    const load = (file: unknown, hash: unknown) => {
      if (typeof file !== 'string' || typeof hash !== 'string' || !/^[a-f0-9]{64}$/.test(hash)) throw new Error();
      if (!isAbsolute(file) || statSync(file).size > 64 * 1024 * 1024) throw new Error();
      const bytes = readFileSync(file);
      if (createHash('sha256').update(bytes).digest('hex') !== hash) throw new Error();
      const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      if (!object(value)) throw new Error();
      return value;
    };
    const tokenizer = new Tokenizer(load(config.tokenizer_file, config.tokenizer_sha256), load(config.config_file, config.config_sha256));
    const counterId = 'local-tokenizer-v1-' + createHash('sha256').update(JSON.stringify([
      config.model_id, config.tokenizer_sha256, config.config_sha256, 'tokenizers-js-0.2.0',
    ])).digest('hex');
    return { modelId: config.model_id, counterId,
      count: (text: string) => tokenizer.encode(text, { add_special_tokens: false }).ids.length };
  } catch { throw new Error('evernook:tokenizer_unavailable'); }
}
