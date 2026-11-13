// Demonstrate the host API boundary; this does not enable reliable tool publication.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const require = createRequire(join(process.argv[2], 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await load('@deepseek-ai/cordis');
const { default: Prompt } = await load('@deepseek-ai/dsh-system-prompt');
const { default: Tools, defineContentToolFixture } = await load('@deepseek-ai/dsh-tools');
const ctx = new Context();
try {
  await ctx.plugin(Prompt, { includeHarnessIdentity: false }).await();
  await ctx.plugin(Tools).await();
  ctx.tools.register(defineContentToolFixture({ name: 'synthetic-publication', description: 'Synthetic probe only',
    parameters: {}, execute: async () => [{ type: 'text', text: 'synthetic final result' }] }));
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let observed = false;
  let persisted = false;
  const off = ctx.on('tools/result', async () => { observed = true; await blocked; persisted = true; });
  const result = await ctx.tools.execute({ callId: 'synthetic-call', name: 'synthetic-publication', arguments: {},
    signal: AbortSignal.timeout(5000) });
  assert.equal(result.isError, false);
  assert.equal(observed, true);
  assert.equal(persisted, false, 'Update the acceptance probe if the host starts awaiting result observers');
  release();
  await blocked;
  await Promise.resolve();
  assert.equal(persisted, true);
  off();
  const failing = ctx.on('tools/result', () => { throw new Error('synthetic storage unavailable'); });
  const failedObserver = await ctx.tools.execute({ callId: 'synthetic-failure', name: 'synthetic-publication', arguments: {},
    signal: AbortSignal.timeout(5000) });
  assert.equal(failedObserver.isError, false);
  failing();
  console.log(JSON.stringify({ observer_awaited: false, observer_failure_blocks_result: false, reliable_tools_admitted: false }));
} finally { await ctx.fiber.dispose(); }
