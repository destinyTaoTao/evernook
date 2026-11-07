import { spawn } from 'node:child_process';
import type { MemoryConfig } from './client.ts';

type RecordValue = Record<string, unknown>;
export type MaintenanceOperation = 'status' | 'mark-important' | 'change' | 'purge-preview' | 'purge' | 'profile-read' | 'profile-decide'
  | 'candidate-list' | 'fact-query' | 'fact-decide';
export type LocalOperation = MaintenanceOperation | 'scheduler-trigger' | 'scheduler-status' | 'scheduler-session-end' | 'manage' | 'host-recovery';
function object(value: unknown): value is RecordValue { return value !== null && typeof value === 'object' && !Array.isArray(value); }

/** Fixed local CLI operations. Evidence goes through stdin, never a shell or process arguments. */
export function runLocalCli(config: MemoryConfig, access: string, operation: LocalOperation, input: RecordValue,
             signal?: AbortSignal, label: 'maintenance' | 'consolidation' = 'maintenance'): Promise<RecordValue> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const worker = label === 'consolidation';
    const child = spawn(config.python, ['-m', worker ? 'evernook.worker' : 'evernook.cli', operation,
      '--config', config.config, '--access', access],
      { cwd: config.cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'],
        env: worker ? { ...process.env, EVERNOOK_WORKER_PARENT_PID: String(process.pid) } : process.env });
    const chunks: Buffer[] = [];
    let bytes = 0;
    let failure: string | undefined;
    const abort = () => { failure = 'operation_canceled'; child.kill(); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    const timer = setTimeout(() => { failure = label + '_timeout'; child.kill(); }, 30000);
    child.stdout.on('data', (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > 8 * 1024 * 1024) { failure = label + '_result_too_large'; child.kill(); }
      else chunks.push(chunk);
    });
    child.stdin.on('error', () => { failure ??= label + '_unavailable'; });
    child.on('error', () => { failure = label + '_unavailable'; });
    child.on('close', code => {
      clearTimeout(timer); signal?.removeEventListener('abort', abort);
      if (failure) { reject(new Error('evernook:' + failure)); return; }
      try {
        const payload: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
        if (!object(payload)) throw new Error();
        if (code !== 0 || payload.ok !== true) {
          const reason = object(payload.error) && typeof payload.error.code === 'string' && /^[a-z_]{1,80}$/.test(payload.error.code)
            ? payload.error.code : label + '_unavailable';
          reject(new Error('evernook:' + reason)); return;
        }
        if (!object(payload.result)) throw new Error();
        resolve(payload.result);
      } catch { reject(new Error('evernook:' + label + '_unavailable')); }
    });
    child.stdin.end(JSON.stringify(input));
  });
}
