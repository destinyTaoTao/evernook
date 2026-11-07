import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { realpathSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
assert.ok(process.env.EVERNOOK_DSH_TEST_HOST, 'Set EVERNOOK_DSH_TEST_HOST to an isolated private host');
export const root = realpathSync.native(process.env.EVERNOOK_DSH_TEST_HOST);
const require = createRequire(join(root, 'package.json'));
export const load = name => import(pathToFileURL(require.resolve(name)).href);
export const { Context } = await load('@deepseek-ai/cordis');
export const { default: Prompt } = await load('@deepseek-ai/dsh-system-prompt');
export const { default: Tools, defineContentToolFixture } = await load('@deepseek-ai/dsh-tools');
export const hook = 'evernook/tools/before-publish';
export async function service() {
  const ctx = new Context();
  await ctx.plugin(Prompt, { includeHarnessIdentity: false }).await();
  await ctx.plugin(Tools).await();
  return ctx;
}
export function fixture(name = 'synthetic-tool', execute = async () => [{ type: 'text', text: '合成最终结果🙂\r\n37.50元。' }]) {
  return defineContentToolFixture({ name, description: 'Synthetic publication fixture', parameters: {}, execute });
}
export function call(ctx, name = 'synthetic-tool', extra = {}) {
  return ctx.tools.execute({ callId: 'synthetic-call', name, arguments: {}, signal: AbortSignal.timeout(5000), ...extra });
}
