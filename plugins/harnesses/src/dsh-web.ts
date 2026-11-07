import { randomBytes, timingSafeEqual } from 'node:crypto';
import { isAbsolute } from 'node:path';
import type { Context } from '@deepseek-ai/cordis';
import z from '@deepseek-ai/schemastery';
import { installMemory, type Config as MemoryOptions } from './dsh.ts';
import { runLocalCli } from './local-cli.ts';
import { MemoryClient } from './client.ts';

export const name = 'dsh-evernook';
export const inject = ['connection', 'sessions', 'sessionPersistence', 'systemPrompt', 'llm'];
export interface Config {
  python: string; config: string; access: string; maintenanceAccess: string; entry: string;
  streamMode: 'buffered' | 'durable_text';
}
export const Config: z<Config> = z.object({
  python: z.string().default(''), config: z.string().default(''), access: z.string().default(''),
  maintenanceAccess: z.string().default(''), entry: z.string().default('dsh'),
  streamMode: z.union(['durable_text', 'buffered']).default('durable_text'),
});
interface Connection {
  fetch: { register(route: { path: string; methods: readonly string[]; requestBody: 'streaming';
    fetch(request: Request): Promise<Response> }): () => void };
}
type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const json = (value: unknown, status = 200) => Response.json(value, { status,
  headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });

/** Owner-only dsh /api Fetch route. No new HTTP listener and no credentials in the browser. */
export async function installWeb(ctx: Context, config: Config) {
  const lifetime = new AbortController();
  let closed = false;
  let memory: Awaited<ReturnType<typeof installMemory>> | undefined;
  const running = new Set<Promise<unknown>>();
  const csrf = randomBytes(32).toString('hex');
  const configured = [config.python, config.config, config.access, config.maintenanceAccess].every(Boolean);
  if (!configured && [config.python, config.config, config.access, config.maintenanceAccess].some(Boolean)) {
    throw new Error('evernook:incomplete_web_config');
  }
  const options: MemoryOptions = { python: config.python, config: config.config, access: config.access,
    entry: config.entry, streamMode: config.streamMode };
  async function manage(input: RecordValue, signal?: AbortSignal) {
    if (running.size >= 4) throw new Error('evernook:management_busy');
    const promise = runLocalCli(options, config.maintenanceAccess, 'manage', input,
      signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal);
    running.add(promise);
    try { return await promise; } finally { running.delete(promise); }
  }
  if (configured) {
    if (![config.python, config.config, config.access, config.maintenanceAccess].every(isAbsolute)
        || config.access === config.maintenanceAccess) throw new Error('evernook:invalid_web_config');
    const ordinary = new MemoryClient(options);
    try {
      await ordinary.connect();
      const actor = await ordinary.call<RecordValue>('status', {});
      const privileged = await runLocalCli(options, config.maintenanceAccess, 'status', {});
      const a = actor.capabilities, b = privileged.capabilities;
      const scopes = object(b) && Array.isArray(b.scopes) ? b.scopes : [];
      if (!object(a) || !object(b) || a.client_id === b.client_id || !Array.isArray(a.scopes)
          || a.scopes.some(scope => !['capture', 'recall'].includes(scope)) || !Array.isArray(b.scopes)
          || !['recall', 'maintain', 'accept'].every(scope => scopes.includes(scope))) {
        throw new Error('evernook:invalid_maintenance_identity');
      }
      await manage({ action: 'read', view: 'status' });
    } finally { await ordinary.close(); }
    memory = await installMemory(ctx, options);
  }
  async function handle(request: Request): Promise<Response> {
    if (closed) return json({ error: 'adapter_closed' }, 503);
    // dsh's HTTP bridge uses a synthetic http://dsh.internal Request URL.
    // Original Host/Origin survive in headers; the owning /api carrier already
    // authenticated the owner and applied its own Host/Origin fence.
    let origin: URL;
    try { origin = new URL(request.headers.get('origin') ?? ''); }
    catch { return json({ error: 'forbidden' }, 403); }
    // This preview is local-only even if dsh has remote trustedHosts configured.
    if (!['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname)
        || !['http:', 'https:'].includes(origin.protocol)
        || request.headers.get('origin') !== origin.origin
        || origin.host !== request.headers.get('host')
        || ['cross-site', 'same-site'].includes(request.headers.get('sec-fetch-site') ?? '')
        || request.headers.get('content-type')?.split(';')[0].trim() !== 'application/json') {
      return json({ error: 'forbidden' }, 403);
    }
    try {
      // Streaming registration enforces our bound before collecting a large body.
      if (Number(request.headers.get('content-length') ?? 0) > 131072) return json({ error: 'request_too_large' }, 413);
      const reader = request.body?.getReader();
      if (!reader) return json({ error: 'invalid_request' }, 400);
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > 131072) { await reader.cancel(); return json({ error: 'request_too_large' }, 413); }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
      const input: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
      if (!object(input)) return json({ error: 'invalid_request' }, 400);
      if (input.action === 'bootstrap' && Object.keys(input).length === 1) {
        return json({ csrf, configured, preview: true, daily_admitted: false, stream_mode: config.streamMode });
      }
      const supplied = request.headers.get('x-evernook-csrf') ?? '';
      if (!/^[a-f0-9]{64}$/.test(supplied) || !timingSafeEqual(Buffer.from(supplied), Buffer.from(csrf))) {
        return json({ error: 'forbidden' }, 403);
      }
      if (!configured) return json({ error: 'configuration_required' }, 409);
      if (input.action === 'session' && typeof input.session_id === 'string' && Object.keys(input).length === 2) {
        if (!/^[\w:.-]{1,256}$/.test(input.session_id)) return json({ error: 'invalid_session' }, 400);
        return json({ result: memory?.status(input.session_id as Parameters<NonNullable<typeof memory>['status']>[0]) });
      }
      if (!['read', 'preview', 'confirm', 'cancel'].includes(String(input.action))) return json({ error: 'invalid_action' }, 400);
      return json({ result: await manage(input, request.signal) });
    } catch (error) {
      const code = error instanceof Error ? /^evernook:([a-z_]{1,80})$/.exec(error.message)?.[1] : undefined;
      return json({ error: code ?? 'management_unavailable' }, 400);
    }
  }
  const connection = ctx.get('connection') as Connection | undefined;
  let unregister: (() => void) | undefined;
  try {
    if (!connection?.fetch?.register) throw new Error('evernook:web_connection_unavailable');
    unregister = connection.fetch.register({ path: '/api/evernook/manage', methods: ['POST'], requestBody: 'streaming', fetch: handle });
  } catch (error) { lifetime.abort(); await memory?.close(); throw error; }
  return { handle, async close() {
    if (closed) return;
    closed = true; unregister?.(); lifetime.abort();
    await Promise.allSettled([...running]);
    await memory?.close();
  } };
}

export async function apply(ctx: Context, config: Config) {
  const installed = await installWeb(ctx, config);
  ctx.effect(() => () => installed.close());
}

export default { name, inject, Config, apply };
