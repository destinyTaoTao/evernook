import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomBytes } from 'node:crypto';

type Config = { python: string; config: string; access: string };
type Pending = { count: number; resolve(value: Record<string, unknown>): void; reject(error: Error): void;
  timer: ReturnType<typeof setTimeout>; signal?: AbortSignal; abort(): void };

/** Read-only worker. Every failure rejects pending reads; there is no cached result. */
export class FingerprintBridge {
  private child?: ChildProcessWithoutNullStreams;
  private buffer = Buffer.alloc(0);
  private readonly pending = new Map<string, Pending>();
  private closed = false;
  private readonly config: Config;
  constructor(config: Config) { this.config = config; }

  private start(): ChildProcessWithoutNullStreams {
    if (this.child) return this.child;
    const child = spawn(this.config.python, ['-B', '-m', 'evernook.fingerprint_worker', '--config', this.config.config,
      '--access', this.config.access], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, EVERNOOK_WORKER_PARENT_PID: String(process.pid) } });
    this.child = child; this.buffer = Buffer.alloc(0);
    child.stderr.resume(); // Never copy diagnostics or request contents into host logs.
    const fail = () => this.fail(child);
    child.on('error', fail); child.on('close', fail); child.stdin.on('error', fail); child.stdout.on('error', fail);
    child.stdout.on('data', (chunk: Buffer) => {
      if (this.child !== child) return;
      this.buffer = Buffer.concat([this.buffer, chunk]);
      if (this.buffer.length > 131072) { this.fail(child); return; }
      let end: number;
      while ((end = this.buffer.indexOf(10)) >= 0) {
        const line = this.buffer.subarray(0, end); this.buffer = this.buffer.subarray(end + 1);
        try {
          const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
          if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
          const response = value as Record<string, unknown>;
          if (typeof response.id !== 'string' || !/^[a-f0-9]{32}$/.test(response.id)) throw new Error();
          const item = this.pending.get(response.id);
          if (!item) continue; // Canceled request may finish after its caller has left.
          if (Object.keys(response).length !== 2) throw new Error();
          if (response.error === 'fingerprint_unavailable') this.finish(response.id, new Error('evernook:fingerprint_unavailable'));
          else if (Array.isArray(response.tokens) && response.tokens.length === item.count
              && response.tokens.every(token => typeof token === 'string' && /^[a-f0-9]{64}$/.test(token))) {
            this.finish(response.id, undefined, { tokens: response.tokens });
          } else throw new Error();
        } catch { this.fail(child); return; }
      }
    });
    return child;
  }

  private finish(id: string, error?: Error, value?: Record<string, unknown>): void {
    const item = this.pending.get(id);
    if (!item) return;
    this.pending.delete(id); clearTimeout(item.timer); item.signal?.removeEventListener('abort', item.abort);
    if (error) item.reject(error); else item.resolve(value!);
  }

  private fail(child: ChildProcessWithoutNullStreams): void {
    if (this.child !== child) return;
    this.child = undefined; child.kill();
    for (const id of this.pending.keys()) this.finish(id, new Error('evernook:fingerprint_unavailable'));
  }

  request(requests: unknown, signal?: AbortSignal): Promise<Record<string, unknown>> {
    signal?.throwIfAborted();
    if (this.closed || !Array.isArray(requests) || requests.length < 1 || requests.length > 3 || this.pending.size >= 4) {
      return Promise.reject(new Error('evernook:fingerprint_unavailable'));
    }
    const id = randomBytes(16).toString('hex');
    const payload = JSON.stringify({ id, requests }) + '\n';
    if (Buffer.byteLength(payload) > 131072) return Promise.reject(new Error('evernook:fingerprint_unavailable'));
    const child = this.start();
    return new Promise((resolve, reject) => {
      const abort = () => {
        if (this.child === child && child.stdin.writable) child.stdin.write(JSON.stringify({ id, cancel: true }) + '\n');
        this.finish(id, new Error('evernook:fingerprint_unavailable'));
      };
      const timer = setTimeout(abort, 9000);
      this.pending.set(id, { count: requests.length, resolve, reject, signal, abort, timer });
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort(); else child.stdin.write(payload);
    });
  }

  close(): void {
    this.closed = true;
    if (this.child) this.fail(this.child);
  }
}
