import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync, writeFileSync, renameSync, existsSync, unlinkSync } from 'node:fs';
import { join, resolve, dirname, relative, sep, isAbsolute } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { publicationPatch, transformPublicationSource } from './publication-patch.mjs';

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
function targets(root) {
  root = realpathSync.native(root);
  const checkout = realpathSync.native(resolve(dirname(fileURLToPath(import.meta.url)), '../../..'));
  const external = relative(checkout, root);
  assert.ok(external.startsWith('..' + sep) || isAbsolute(external), 'Publication patch host must be outside the checkout');
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  assert.equal(manifest.name, 'evernook-dsh-verified-host', 'Only the dedicated private host may be patched');
  assert.equal(manifest.private, true);
  assert.equal(manifest.dependencies['@deepseek-ai/dsh'], '0.2.1-rc.5');
  return publicationPatch.map(patch => {
    const directory = join(root, 'node_modules', patch.package);
    assert.equal(JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8')).version, '0.2.1-rc.5');
    const path = realpathSync.native(join(directory, patch.file));
    const within = relative(root, path);
    assert.ok(within && !within.startsWith('..' + sep) && !isAbsolute(within), 'Refusing a dependency linked outside the private host');
    return { ...patch, path };
  });
}

export function verifyPublicationPatch(root) {
  for (const patch of targets(root)) {
    assert.equal(digest(readFileSync(patch.path)), patch.after, `Publication patch missing or changed: ${patch.package}`);
  }
}

export function applyPublicationPatch(root) {
  // Validate every original and transformed file before modifying any of them.
  const changes = targets(root).flatMap(patch => {
    const original = readFileSync(patch.path);
    if (digest(original) === patch.after) return [];
    assert.equal(digest(original), patch.before, `Unrecognized upstream artifact: ${patch.package}`);
    const source = transformPublicationSource(original.toString('utf8'), patch.hunks);
    assert.equal(digest(source), patch.after, 'Transformed publication patch hash differs');
    return [{ path: patch.path, source }];
  });
  for (const change of changes) {
    const temporary = change.path + '.evernook-publication.tmp';
    let owned = false;
    try {
      writeFileSync(temporary, change.source, { encoding: 'utf8', flag: 'wx' });
      owned = true;
      renameSync(temporary, change.path);
    } finally { if (owned && existsSync(temporary)) unlinkSync(temporary); }
  }
  verifyPublicationPatch(root);
  return { publication_patch: 1, modified_files: changes.length };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  assert.ok(process.argv[2], 'Usage: node patch.mjs <stopped dedicated private host directory>');
  console.log(JSON.stringify(applyPublicationPatch(process.argv[2])));
}
