import { execFileSync } from 'node:child_process';
import { cpSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const root = new URL('../', import.meta.url);
const harness = new URL('../../harnesses/', import.meta.url);
execFileSync(process.execPath, [fileURLToPath(new URL('node_modules/typescript/bin/tsc', harness)), '-p', 'tsconfig.build.json'],
  { cwd: fileURLToPath(harness), stdio: 'inherit' });
mkdirSync(new URL('lib/', root), { recursive: true });
cpSync(new URL('dist/', harness), new URL('lib/', root), { recursive: true });
cpSync(new URL('../../LICENSE', root), new URL('LICENSE', root));
