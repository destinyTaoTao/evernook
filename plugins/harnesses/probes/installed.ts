import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { Socket } from 'node:net';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';
import type { MemoryClient as ClientType, Event } from '../src/client.ts';

const [hostRoot, python, root, policyPath, pilotSeconds] = process.argv.slice(2);
if (![hostRoot, python, root, policyPath].every(Boolean)) throw new Error('installation_arguments_required');
const installedRequire = createRequire(join(hostRoot, 'package.json'));
const installedPath = installedRequire.resolve('@evernook/harnesses/client');
const installed = await import(pathToFileURL(installedPath).href) as typeof import('../src/client.ts');
const { Client } = installedRequire('@modelcontextprotocol/client') as typeof import('@modelcontextprotocol/client');
const { StdioClientTransport } = installedRequire('@modelcontextprotocol/client/stdio') as typeof import('@modelcontextprotocol/client/stdio');
const policyText = await readFile(policyPath, 'utf8');
const policy = JSON.parse(policyText);
const seconds = pilotSeconds ? Number(pilotSeconds) : policy.observation_seconds;
if (!Number.isSafeInteger(seconds) || seconds < 10 || seconds > policy.observation_seconds) throw new Error('invalid_window');
const fixture = JSON.parse(await readFile(join(root, 'fixture.json'), 'utf8'));
if (fixture.core_version !== policy.core_minimum_version || process.versions.node !== policy.node) throw new Error('installed_version_mismatch');
let networkAttempts = 0;
globalThis.fetch = async () => { networkAttempts++; throw new Error('offline'); };
Socket.prototype.connect = function () { networkAttempts++; throw new Error('offline'); } as typeof Socket.prototype.connect;
const config = { python, config: fixture.config, access: join(root, 'runtime/mcp-access-installed-harness.json'),
  entry: 'installed-harness', cwd: root };
let writer: ClientType = new installed.MemoryClient(config);
const reader = new installed.MemoryClient(config);
const records: Record<string, unknown>[] = [];
const errors: string[] = [];
const started = performance.now();
let maintenance: ReturnType<typeof spawn> | undefined;
let forcedExitVerified = false;
let restartVerified = false;
let canceledVerified = false;
let report: Record<string, unknown> = {};
const save = async () => {
  report = { phase: 'installed_mcp_longrun', pilot: Boolean(pilotSeconds), requested_seconds: seconds,
    elapsed_seconds: (performance.now() - started) / 1000, installed_client: installedPath,
    installed_client_sha256: createHash('sha256').update(await readFile(installedPath)).digest('hex'),
    policy_sha256: createHash('sha256').update(policyText).digest('hex'),
    probe_sha256: createHash('sha256').update(await readFile(import.meta.filename)).digest('hex'),
    records, errors, forced_service_exit_verified: forcedExitVerified, client_restart_verified: restartVerified,
    canceled_stream_verified: canceledVerified, network_attempts: networkAttempts,
    scope: 'Installed npm facade and Python wheel over real MCP. Synthetic public API workload and concurrent navigation/index maintenance. No model, native UI, physical OS offline/sleep or power-loss claim.' };
  await writeFile(join(root, 'mcp-longrun.json'), JSON.stringify(report, null, 2) + '\n');
};

try {
  await save();
  await writer.connect(); await reader.connect();
  const status = await writer.call<{ capabilities: { scopes: string[]; vault: boolean } }>('status', {});
  if (status.capabilities.scopes.join(',') !== 'capture,recall' || status.capabilities.vault) throw new Error('installed_scope_or_vault_mismatch');
  maintenance = spawn(python, [resolve(import.meta.dirname, '../../../probes/installed/fixture.py'), 'maintain',
    '--output', root, '--seconds', String(seconds)], { cwd: root, windowsHide: true, stdio: 'ignore',
    env: { ...process.env, PYTHONPATH: '' } });
  for (let index = 0; performance.now() - started < seconds * 1000; index++) {
    const tick = performance.now();
    try {
      const request: Event = { source: { entry: config.entry, session: 'synthetic-installed-longrun', branch: 'main', event: 'input:' + index },
        role: 'user', sequence: index * 2, occurred_at: '2026-11-04T00:00:00Z', completion: 'complete', text: '合成第' + index + '轮：37.50元，🙂。' };
      const receipt = await writer.remember(request);
      if (!receipt.original_saved || (await reader.read(receipt.message_id)).event.text !== request.text) throw new Error('installed_original_roundtrip');
      const source = { ...request.source, event: 'reply:' + index };
      const first = '合成已保存的首块🙂。';
      const event: Event = { ...request, source, role: 'assistant', sequence: index * 2 + 1, completion: 'partial', text: first };
      const ack = await writer.appendChunk(event, 0, '', receipt.message_id);
      const prefix = await reader.readStream(source);
      if (prefix.text !== Array.from(first).slice(0, ack.released_chars).join('')) throw new Error('installed_prefix_before_publication');
      const last = '合成终块' + index + '。';
      await writer.appendChunk({ ...event, completion: 'complete', text: last }, 1, '', receipt.message_id);
      const final = await reader.readStream(source);
      if (!final.complete || final.text !== first + last) throw new Error('installed_final_replay');
      if (!canceledVerified) {
        const canceled = { ...event, source: { ...source, event: 'canceled:' + index } };
        await writer.appendChunk(canceled, 0, '', receipt.message_id);
        await writer.appendChunk({ ...canceled, completion: 'canceled', text: '' }, 1, '', receipt.message_id);
        const value = await reader.readStream(canceled.source);
        if (value.complete || value.state !== 'canceled') throw new Error('installed_cancellation_state');
        canceledVerified = true;
      }
      if (!forcedExitVerified && performance.now() - started >= seconds * 1000 / 3) {
        const peer = new Client({ name: 'installed-owned-service-exit', version: '1.0.0' });
        const transport = new StdioClientTransport({ command: python,
          args: ['-m', 'evernook.mcp_server', '--config', config.config, '--access', config.access, '--profile', 'trusted-host'], cwd: root, stderr: 'ignore' });
        try {
          await peer.connect(transport);
          await peer.callTool({ name: 'evernook_v1_status', arguments: {} });
          const pid = transport.pid;
          if (!pid) throw new Error('owned_service_pid_unavailable');
          // The live transport owns this process; no stale PID lookup or unrelated process enumeration.
          process.kill(pid);
          await delay(250);
          let rejected = false;
          try { await peer.callTool({ name: 'evernook_v1_status', arguments: {} }, { signal: AbortSignal.timeout(2000) }); }
          catch { rejected = true; }
          if (!rejected || (await reader.read(receipt.message_id)).event.text !== request.text) throw new Error('installed_service_exit_not_verified');
          forcedExitVerified = true;
        } finally { await peer.close(); }
      }
      if (!restartVerified && performance.now() - started >= seconds * 2000 / 3) {
        await writer.close(); writer = new installed.MemoryClient(config); await writer.connect();
        if ((await writer.read(receipt.message_id)).event.text !== request.text
            || (await writer.readStream(source)).text !== first + last) throw new Error('installed_restart_replay');
        restartVerified = true;
      }
      records.push({ index, elapsed_seconds: (performance.now() - started) / 1000,
        roundtrip_ms: performance.now() - tick, original_id: receipt.message_id, prefix_chars: ack.released_chars,
        full_stream_chars: Array.from(final.text).length, replay_complete: final.complete });
    } catch (error) { errors.push(error instanceof Error ? error.message.slice(0, 160) : 'installed_iteration_failed'); }
    await save();
    await delay(Math.max(0, policy.turn_interval_seconds * 1000 - (performance.now() - tick)));
  }
} catch (failure) {
  errors.push('startup:' + (failure instanceof Error ? failure.message.slice(0, 160) : 'installed_startup_failed'));
} finally {
  await writer.close(); await reader.close();
  await writeFile(join(root, 'maintenance-stop'), 'stop\n');
  if (maintenance?.exitCode === null) await once(maintenance, 'exit');
  if (maintenance && maintenance.exitCode !== 0) errors.push('installed_maintenance_failed');
  if (!forcedExitVerified || !restartVerified || !canceledVerified) errors.push('installed_lifecycle_check_missing');
  await save();
}
process.exitCode = errors.length || networkAttempts ? 1 : 0;
