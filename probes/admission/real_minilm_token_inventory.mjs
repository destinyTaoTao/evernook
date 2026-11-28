// Count the production tokenizer's distinct unpadded inputs in the frozen million.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';

const [root, configFile] = process.argv.slice(2);
const config = JSON.parse(readFileSync(configFile, 'utf8'));
const manifest = JSON.parse(readFileSync(join(root, 'source-manifest.json'), 'utf8'));
if (manifest.expected_messages !== 1_000_000) throw new Error('unexpected fixture');
const { AutoTokenizer, env } = await import(pathToFileURL(join(config.transformers_dir, 'dist/transformers.node.mjs')));
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.useFSCache = false;
const tokenizer = await AutoTokenizer.from_pretrained(config.model_dir, { local_files_only: true });
const db = new DatabaseSync(join(root, 'runtime', 'memory.sqlite3'), { readOnly: true });
const hashes = new Set();
const digest = createHash('sha256');
const lengths = {};
let processed = 0;
const started = performance.now();
try {
  // Short read transactions let the writer checkpoint while this inventory runs.
  const window = db.prepare('SELECT rowid,event FROM messages WHERE rowid>? AND rowid<=? ORDER BY rowid');
  while (processed < manifest.expected_messages) {
    const rows = window.all(processed, Math.min(processed + 2000, manifest.expected_messages));
    if (rows.length === 0) throw new Error('missing source');
    for (const { rowid, event } of rows) {
      const source = JSON.parse(event);
      if (rowid !== processed + 1 || source.sequence !== processed || source.source.entry !== 'synthetic-scale'
          || source.source.event !== 'm' + String(processed).padStart(7, '0')) throw new Error('source mismatch');
      const ids = tokenizer(source.text, { truncation: false, padding: false }).input_ids.data;
      const bytes = Buffer.alloc(ids.length * 4);
      for (let i = 0; i < ids.length; i++) bytes.writeUInt32LE(Number(ids[i]), i * 4);
      const hash = createHash('sha256').update(bytes).digest();
      hashes.add(hash.toString('base64'));
      digest.update(hash);
      lengths[ids.length] = (lengths[ids.length] || 0) + 1;
      processed++;
    }
  }
} finally {
  db.close();
}
writeFileSync(join(root, 'token-inventory.json'), JSON.stringify({
  source_count: processed, distinct_token_sha256: hashes.size, source_order_token_inventory_sha256: digest.digest('hex'),
  token_lengths: lengths, tokenizer_sha256: createHash('sha256').update(readFileSync(join(config.model_dir, 'tokenizer.json'))).digest('hex'),
  elapsed_seconds: (performance.now() - started) / 1000, node: process.version
}, null, 2), { flag: 'wx' });
