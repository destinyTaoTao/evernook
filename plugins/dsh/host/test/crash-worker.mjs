import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture, hook } from './runtime.mjs';
import { loop, follow } from './loop.mjs';
const host = await loop();
const handle = await host.create();
host.ctx.tools.register(fixture('synthetic-tool', async () => {
  await writeFile(join(host.root, 'synthetic-effect.txt'), 'synthetic side effect happened once');
  return [{ type: 'text', text: 'synthetic unsaved final result' }];
}));
host.ctx.on(hook, async () => {
  await host.ctx.sessions.flush(handle.agent.session);
  process.send({ root: host.root, id: String(handle.agent.session.id) });
  await new Promise(() => {});
});
follow(handle);
// Keep the child alive at the real awaited boundary until the parent terminates it.
setInterval(() => {}, 1000);
