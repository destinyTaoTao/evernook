// Experimental local-only memory reasoning. No execution tools, logging or remote models.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { Socket } from 'node:net';
import { completeObject } from './reflection_json.mjs';

let attempts = 0;
globalThis.fetch = async () => { attempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { attempts++; throw new Error('offline'); };
console.log = console.warn = console.error = () => {};
try {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major !== 24 || minor < 11) throw new Error('version');
  const chunks = []; let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 262144) throw new Error('input');
    chunks.push(chunk);
  }
  const input = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
  if (!Number.isInteger(input.input_tokens) || input.input_tokens < 1 || input.input_tokens > 4096
      || !Number.isInteger(input.output_tokens) || input.output_tokens < 1 || input.output_tokens > 512) throw new Error('budget');
  const info = JSON.parse(readFileSync(join(input.transformers_dir, 'package.json'), 'utf8'));
  if (info.name !== '@huggingface/transformers' || info.version !== '4.2.0') throw new Error('version');
  const require = createRequire(pathToFileURL(join(input.transformers_dir, 'package.json')));
  if (JSON.parse(readFileSync(require.resolve('onnxruntime-node/package.json'), 'utf8')).version !== '1.24.3') throw new Error('version');
  const { env, pipeline, StoppingCriteria, StoppingCriteriaList } = await import(pathToFileURL(join(input.transformers_dir, 'dist/transformers.node.mjs')).href);
  env.allowRemoteModels = false; env.allowLocalModels = true; env.useBrowserCache = false; env.useFSCache = false;
  const generator = await pipeline('text-generation', input.model_dir, { local_files_only: true,
    dtype: 'q4', device: 'cpu', session_options: { intraOpNumThreads: 4, interOpNumThreads: 1 } });
  try {
    if (!Array.isArray(input.messages) || input.messages.length < 2 || input.messages.length > 10
        || input.messages.some(item => !['system', 'user', 'assistant'].includes(item.role) || typeof item.content !== 'string')) throw new Error('messages');
    const prompt = generator.tokenizer.apply_chat_template(input.messages,
      { tokenize: false, add_generation_prompt: true, enable_thinking: false });
    const encoded = generator.tokenizer(prompt, { add_special_tokens: false, truncation: false, padding: false });
    const tokenCount = encoded.input_ids.dims.at(-1);
    if (tokenCount > input.input_tokens) {
      process.stdout.write(JSON.stringify({ status: 'input_budget', action_json: '', input_tokens: tokenCount, output_tokens: 0, network_attempts: attempts }) + '\n');
    } else {
      // Direct generate retains the actual generated IDs, including EOS, for exact metering.
      const criteria = new StoppingCriteriaList();
      criteria.push(new class extends StoppingCriteria {
        _call(rows) { return rows.map(row => completeObject(generator.tokenizer.decode(
          row.slice(tokenCount).map(Number), { skip_special_tokens: true }))); }
      });
      const eos = JSON.parse(readFileSync(join(input.model_dir, 'generation_config.json'), 'utf8')).eos_token_id;
      if (!Array.isArray(eos) || !eos.length || eos.some(id => !Number.isInteger(id) || id < 0)) throw new Error('eos');
      // Suppress premature EOS; stop on an actual complete object or the unchanged hard token cap.
      const output = await generator.model.generate({ ...encoded, max_new_tokens: input.output_tokens, do_sample: false,
        suppress_tokens: eos, stopping_criteria: criteria });
      const tokens = output.dims.at(-1) - tokenCount;
      const decodedPrompt = generator.tokenizer.batch_decode(encoded.input_ids, { skip_special_tokens: true })[0];
      const text = generator.tokenizer.batch_decode(output, { skip_special_tokens: true })[0].slice(decodedPrompt.length);
      if (typeof text !== 'string' || Buffer.byteLength(text) > 65536 || attempts) throw new Error('output');
      if (tokens > input.output_tokens) throw new Error('output_budget');
      process.stdout.write(JSON.stringify({ status: 'ok', action_json: text, input_tokens: tokenCount, output_tokens: tokens, network_attempts: attempts }) + '\n');
    }
  } finally { await generator.dispose(); }
} catch { process.exitCode = 1; }
