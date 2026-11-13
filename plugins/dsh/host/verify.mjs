import assert from 'node:assert/strict';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { verifyPublicationPatch } from './patch.mjs';

export const fixes = Object.freeze({ zod: '4.6.7', undici: '8.12.4', fflate: '0.8.3', '@modelcontextprotocol/client': '2.4.0' });
const json = path => JSON.parse(readFileSync(path, 'utf8'));

function installedPackage(anchor, name) {
  const require = createRequire(anchor);
  let directory = dirname(realpathSync.native(require.resolve(name)));
  for (;;) {
    const manifest = join(directory, 'package.json');
    if (existsSync(manifest)) {
      const value = json(manifest);
      if (value.name === name) return { path: directory, version: value.version };
    }
    const parent = dirname(directory);
    assert.notEqual(parent, directory, `Cannot identify resolved package ${name}`);
    directory = parent;
  }
}

export async function verifyHost(root, profile) {
  const [major, minor] = process.versions.node.split('.').map(Number);
  assert.ok(major === 24 && minor >= 11, 'Verified host requires Node >=24.11 <25');
  root = realpathSync.native(root);
  const manifest = json(join(root, 'package.json'));
  const lock = json(join(root, 'package-lock.json'));
  assert.equal(manifest.dependencies['@deepseek-ai/dsh'], '0.2.1-rc.5');
  assert.deepEqual(manifest.overrides, fixes, 'The reviewed overrides must match exactly');
  for (const [path, value] of Object.entries(lock.packages)) {
    const name = path.split('node_modules/').at(-1);
    if (fixes[name]) assert.equal(value.version, fixes[name], `Unsafe locked copy: ${path}`);
  }
  let checked = 0;
  const consumers = Object.entries(lock.packages).filter(([path]) => path && existsSync(join(root, path, 'package.json')));
  // Resolve from every installed consumer, rather than trusting a top-level version.
  for (const [path, value] of consumers) {
    for (const name of Object.keys(value.dependencies ?? {})) {
      if (!fixes[name]) continue;
      assert.equal(installedPackage(join(root, path, 'package.json'), name).version, fixes[name], `${path} -> ${name}`);
      checked++;
    }
  }
  const cli = json(join(root, 'node_modules/@deepseek-ai/dsh/package.json'));
  assert.equal(cli.version, '0.2.1-rc.5');
  assert.ok(checked >= 50, 'Expected dsh consumers were not checked');
  verifyPublicationPatch(root);
  if (profile) {
    const config = json(join(profile, 'package.json'));
    assert.ok(config.dependencies?.['dsh-evernook'], 'Profile must have the preview installed');
    const anchor = join(profile, 'node_modules/dsh-evernook/package.json');
    for (const name of ['zod', '@modelcontextprotocol/client']) {
      assert.equal(installedPackage(anchor, name).version, fixes[name], `Profile plugin -> ${name}`);
    }
    // dsh resolves its peers through its host loader; pnpm must not install old peer copies.
    const yaml = readFileSync(join(profile, 'pnpm-workspace.yaml'), 'utf8');
    assert.match(yaml, /^autoInstallPeers:\s*false\s*$/m, 'Profile must disable automatic peer installation');
  }
  console.log(JSON.stringify({ host: cli.version, publication_patch: 1, checked_dependency_edges: checked, profile_checked: Boolean(profile), fixes }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  assert.ok(process.argv[2], 'Usage: node verify.mjs <host directory> [profile directory]');
  await verifyHost(process.argv[2], process.argv[3]);
}
