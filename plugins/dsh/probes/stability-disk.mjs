// Logical live-tree size. Files unlinked during traversal no longer consume named-file space.
import fs from 'node:fs/promises';
import { join } from 'node:path';

export async function disk(path) {
  let bytes = 0;
  for (const value of await fs.readdir(path, { withFileTypes: true })) {
    const child = join(path, value.name);
    let info;
    try { info = await fs.lstat(child); }
    catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    if (info.isSymbolicLink()) continue;
    if (info.isDirectory()) {
      try { bytes += await disk(child); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    } else bytes += info.size;
  }
  return bytes;
}
