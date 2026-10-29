import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { cpus, platform, release } from 'node:os';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { assertExternalPath } from './host.ts';

const [flag, output, ...extra] = process.argv.slice(2);
if (flag !== '--output' || !output || extra.length) throw new Error('Usage: npm run probe -- --output <new absolute directory outside checkout>');
assertExternalPath(output);
mkdirSync(output); // Never overwrite earlier evidence runs.
const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const command = ['--test', '--test-timeout=30000', '--test-reporter=spec', 'test/host.test.ts', 'test/crash.test.ts', 'test/display.test.ts'];
const startedAt = new Date().toISOString();
const result = spawnSync(process.execPath, command, {
  cwd: packageRoot, env: { ...process.env, EVERNOOK_PROBE_RUN_ROOT: output, NO_COLOR: '1' },
  encoding: 'utf8', timeout: 120000,
});
writeFileSync(join(output, 'tests.txt'), result.stdout + result.stderr);
const hash = (path: string) => createHash('sha256').update(readFileSync(join(packageRoot, path))).digest('hex');
const dependencies = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).dependencies as Record<string, string>;
const packages = Object.fromEntries(Object.entries(dependencies).map(([name, expectedVersion]) => {
  const installed = JSON.parse(readFileSync(join(packageRoot, 'node_modules', name, 'package.json'), 'utf8'));
  if (installed.version !== expectedVersion) throw new Error(`Dependency drift: ${name}`);
  return [name, { version: installed.version, license: installed.license, sha256: hash(`node_modules/${name}/lib/index.js`) }];
}));
const manifest = {
  startedAt, finishedAt: new Date().toISOString(), node: process.version,
  os: `${platform()} ${release()}`, arch: process.arch, cpu: cpus()[0]?.model,
  dsh: '0.2.1-rc.3', cordis: '4.0.5', model: 'synthetic deterministic adapter; no model downloads or cloud calls',
  host: 'real Cordis/dsh core composition; desktop/CLI default profiles not exercised',
  packages,
  command: [process.execPath, ...command],
  sha256: Object.fromEntries(['package-lock.json', 'src/host.ts', 'src/display-barrier.ts', 'test/host.test.ts', 'test/crash.test.ts', 'test/crash-worker.ts', 'test/display.test.ts'].map(path => [path, hash(path)])),
  exitCode: result.status, signal: result.signal, error: result.error?.message,
  limitations: ['Synthetic data only', 'Process kill is not a power-loss test', 'File references do not archive attachment bytes in the sink', 'No production memory service or GUI acceptance', 'Stream barrier tested at native live-publication event, not installed desktop pixels or arbitrary other plugin order'],
};
writeFileSync(join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
console.log(`Raw synthetic evidence: ${output}`);
process.exitCode = result.status ?? 1;
