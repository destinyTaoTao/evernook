import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { Tokenizer } from '@huggingface/tokenizers';

export async function tokenizerFixture(root: string, modelId: string) {
  const data = { model: { type: 'WordLevel', vocab: { '[UNK]': 0, hello: 1, world: 2 }, unk_token: '[UNK]' },
    pre_tokenizer: { type: 'Whitespace' }, decoder: null, normalizer: null, post_processor: null, added_tokens: [] };
  const text = JSON.stringify(data);
  const tokenizerFile = join(root, 'tokenizer.json');
  const configFile = join(root, 'tokenizer_config.json');
  await writeFile(tokenizerFile, text);
  await writeFile(configFile, '{}');
  const counterFile = join(root, `counter-${modelId}.json`);
  await writeFile(counterFile, JSON.stringify({ schema_version: 1, node: process.execPath,
    package_dir: resolve(import.meta.dirname, '../node_modules/@huggingface/tokenizers'), model_id: modelId,
    tokenizer_file: tokenizerFile, config_file: configFile,
    tokenizer_sha256: createHash('sha256').update(text).digest('hex'), config_sha256: createHash('sha256').update('{}').digest('hex') }));
  const contextConfig = join(root, `context-${modelId}.json`);
  await writeFile(contextConfig, JSON.stringify({ schema_version: 1, tokenizer_config: counterFile,
    policy: { normal_tokens: 512, deep_tokens: 1024, pinned_tokens: 128, recent_days: 7, recent_target_chars: 8192 } }));
  return { contextConfig, target: new Tokenizer(data, {}) };
}
