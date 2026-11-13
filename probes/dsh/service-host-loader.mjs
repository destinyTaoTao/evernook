// Resolve native peers from the isolated verified host during installed-service tests.
import { createRequire, registerHooks } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

assert.ok(process.env.EVERNOOK_DSH_TEST_HOST, 'Set the isolated verified host directory');
const require = createRequire(join(process.env.EVERNOOK_DSH_TEST_HOST, 'package.json'));
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@deepseek-ai/')) {
      return { url: pathToFileURL(require.resolve(specifier)).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});
