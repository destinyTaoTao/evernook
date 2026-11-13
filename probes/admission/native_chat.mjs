// Encode the frozen messages using only the model's own embedded chat template.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const input = JSON.parse(readFileSync(0, 'utf8'));
if (typeof input.template !== 'string' || input.template.length > 65536
    || !Array.isArray(input.messages) || input.messages.length > 10) throw Error('invalid_input');
const { Template } = await import(pathToFileURL(join(input.jinja_dir, 'dist/index.js')).href);
process.stdout.write(new Template(input.template).render({ messages: input.messages,
  add_generation_prompt: true, enable_thinking: input.thinking === true,
  bos_token: '', eos_token: '<|im_end|>' }));
