import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile, symlink } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { root } from './runtime.mjs';
import { publicationPatch } from '../publication-patch.mjs';
import { applyPublicationPatch, verifyPublicationPatch } from '../patch.mjs';

async function copyHost(linkTools = false) {
  await mkdir(process.env.EVERNOOK_DSH_TEST_OUTPUT, { recursive: true });
  const clone = await mkdtemp(join(process.env.EVERNOOK_DSH_TEST_OUTPUT, 'patch-'));
  await writeFile(join(clone, 'package.json'), await readFile(join(root, 'package.json')));
  for (const patch of publicationPatch) {
    const original = join(root, 'node_modules', patch.package);
    const target = join(clone, 'node_modules', patch.package);
    if (linkTools && patch.package === '@deepseek-ai/dsh-tools') {
      await mkdir(dirname(target), { recursive: true });
      if (patch.file === 'lib/index.js') await symlink(original, target, 'junction');
      continue;
    }
    await mkdir(dirname(join(target, patch.file)), { recursive: true });
    await writeFile(join(target, 'package.json'), await readFile(join(original, 'package.json')));
    await writeFile(join(target, patch.file), await readFile(join(original, patch.file)));
  }
  return clone;
}
const hashes = directory => Promise.all(publicationPatch.map(async patch => createHash('sha256')
  .update(await readFile(join(directory, 'node_modules', patch.package, patch.file))).digest('hex')));

test('reapplying the exact patch changes no installed bytes', async () => {
  const clone = await copyHost();
  const before = await hashes(clone);
  assert.equal(applyPublicationPatch(clone).modified_files, 0);
  assert.deepEqual(await hashes(clone), before);
  verifyPublicationPatch(clone);
});

test('a changed upstream file rejects the entire patch before any writes', async () => {
  const clone = await copyHost();
  const first = publicationPatch[0];
  const firstPath = join(clone, 'node_modules', first.package, first.file);
  let upstream = await readFile(firstPath, 'utf8');
  for (const [before, after, count = 1] of [...first.hunks].reverse()) {
    assert.equal(upstream.split(after).length, count + 1);
    upstream = upstream.split(after).join(before);
  }
  assert.equal(createHash('sha256').update(upstream).digest('hex'), first.before);
  await writeFile(firstPath, upstream);
  const target = join(clone, 'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js');
  await writeFile(target, (await readFile(target, 'utf8')) + '\n// synthetic altered upstream\n');
  const before = await hashes(clone);
  assert.throws(() => applyPublicationPatch(clone), /Unrecognized upstream artifact/);
  assert.deepEqual(await hashes(clone), before);
  assert.throws(() => verifyPublicationPatch(clone), /missing or changed/);
});

test('an unexpected dependency version fails before patching', async () => {
  const clone = await copyHost();
  const target = join(clone, 'node_modules/@deepseek-ai/dsh-agent-loop/package.json');
  const value = JSON.parse(await readFile(target, 'utf8')); value.version = '0.2.2-rc.1';
  await writeFile(target, JSON.stringify(value));
  const before = await hashes(clone);
  assert.throws(() => applyPublicationPatch(clone));
  assert.deepEqual(await hashes(clone), before);
});

test('dependencies linked outside the private host are never patched', async () => {
  const clone = await copyHost(true);
  const before = await hashes(root);
  assert.throws(() => applyPublicationPatch(clone), /linked outside/);
  assert.deepEqual(await hashes(root), before);
});

test('only the pinned previous publication artifacts can upgrade to recovery guards', async () => {
  const clone = await copyHost();
  for (const patch of publicationPatch.filter(value => value.previous)) {
    const path = join(clone, 'node_modules', patch.package, patch.file);
    let previous = await readFile(path, 'utf8');
    for (const [before, after] of [...patch.previous.hunks].reverse()) {
      assert.equal(previous.split(after).length, 2);
      previous = previous.replace(after, before);
    }
    assert.equal(createHash('sha256').update(previous).digest('hex'), patch.previous.hash);
    await writeFile(path, previous);
  }
  assert.equal(applyPublicationPatch(clone).modified_files, 2);
  assert.deepEqual(await hashes(clone), await hashes(root));
});
