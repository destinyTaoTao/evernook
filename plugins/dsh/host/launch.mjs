// Use one physical spelling for dsh boot and the loader, including Windows 8.3 aliases.
import assert from 'node:assert/strict';
import { realpathSync } from 'node:fs';
import { isAbsolute, join, resolve, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { verifyHost } from './verify.mjs';

const [host, ...args] = process.argv.slice(2);
assert.ok(host && isAbsolute(host), 'Usage: node launch.mjs <absolute host directory> <dsh arguments>');
assert.ok(process.env.DSH_HOME && isAbsolute(process.env.DSH_HOME), 'Set DSH_HOME to an existing external dedicated directory');
const home = realpathSync.native(process.env.DSH_HOME);
const checkout = realpathSync.native(resolve(dirname(fileURLToPath(import.meta.url)), '../../..'));
const within = relative(checkout, home);
assert.ok(within.startsWith('..' + sep) || isAbsolute(within), 'DSH_HOME must be outside the checkout');
const root = realpathSync.native(host);
await verifyHost(root);
const child = spawn(process.execPath, [join(root, 'node_modules/@deepseek-ai/dsh/lib/bin.js'), ...args], {
  env: { ...process.env, DSH_HOME: home }, stdio: 'inherit',
});
const forward = signal => { if (!child.killed) child.kill(signal); };
const interrupt = () => forward('SIGINT');
const terminate = () => forward('SIGTERM');
process.on('SIGINT', interrupt);
process.on('SIGTERM', terminate);
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', code => {
  process.off('SIGINT', interrupt);
  process.off('SIGTERM', terminate);
  process.exitCode = code ?? 1;
});
