// Install a new private host. Never updates the global CLI or an existing profile.
import assert from 'node:assert/strict';
import { copyFileSync, existsSync, mkdirSync, realpathSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { verifyHost } from './verify.mjs';
import { applyPublicationPatch } from './patch.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const checkout = realpathSync.native(resolve(here, '../../..'));
const destination = process.argv[2];
assert.equal(process.argv.length, 3, 'Usage: npm run provision -- <new absolute external directory>');
assert.ok(destination && isAbsolute(destination), 'Destination must be absolute');
const target = join(realpathSync.native(dirname(resolve(destination))), resolve(destination).split(sep).at(-1));
const inside = relative(checkout, target);
assert.ok(inside.startsWith('..' + sep) || isAbsolute(inside), 'Host must be outside the checkout');
assert.ok(!existsSync(target), 'Destination must not already exist; no in-place update is supported');
const [major, minor] = process.versions.node.split('.').map(Number);
assert.ok(major === 24 && minor >= 11, 'Verified host requires Node >=24.11 <25');
assert.ok(process.env.npm_execpath && existsSync(process.env.npm_execpath), 'Run through npm run provision');
mkdirSync(target);
for (const file of ['package.json', 'package-lock.json']) copyFileSync(join(here, file), join(target, file));
const install = spawnSync(process.execPath, [process.env.npm_execpath, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], {
  cwd: target, stdio: 'inherit', timeout: 180_000,
});
assert.equal(install.status, 0, 'Host install failed/timed out; the partial external directory is retained for diagnosis');
console.log(JSON.stringify(applyPublicationPatch(target)));
await verifyHost(target);
const regression = spawnSync(process.execPath, ['--test', '--test-timeout=20000',
  ...['publication', 'loop', 'crash', 'patch', 'ptc', 'recovery'].map(name => join(here, 'test', name + '.test.mjs'))], {
  cwd: target, stdio: 'inherit', timeout: 30_000,
  env: { ...process.env, EVERNOOK_DSH_TEST_HOST: target, EVERNOOK_DSH_TEST_OUTPUT: join(target, '.evernook-install-probe') },
});
assert.equal(regression.status, 0, 'Publication regression did not pass; this host is not admitted');
const audit = spawnSync(process.execPath, [process.env.npm_execpath, 'audit', '--omit=dev'], {
  cwd: target, stdio: 'inherit', timeout: 60_000,
});
assert.equal(audit.status, 0, 'Audit did not pass; this host is not admitted, including when the registry is unavailable');
console.log('Private host installed and audited. Web preview only; native tools and daily use require separate acceptance.');
