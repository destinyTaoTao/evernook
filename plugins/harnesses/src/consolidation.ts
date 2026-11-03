import { isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';
import { MemoryClient, type MemoryConfig } from './client.ts';
import { runLocalCli, type LocalOperation } from './local-cli.ts';

type RecordValue = Record<string, unknown>;
export interface ConsolidationConfig { access: string; pollMs?: number }
function object(value: unknown): value is RecordValue { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function code(error: unknown): string {
  return error instanceof Error && /^evernook:[a-z_]{1,80}$/.test(error.message)
    ? error.message.slice('evernook:'.length) : 'consolidation_unavailable';
}

/** One host-owned bounded worker. Capture/model identity never receives its maintenance access. */
export async function createConsolidation(config: MemoryConfig, options: ConsolidationConfig) {
  config = { ...config }; options = { ...options };
  const pollMs = options.pollMs ?? 5000;
  if (!isAbsolute(options.access) || options.access === config.access || !Number.isSafeInteger(pollMs)
      || pollMs < 100 || pollMs > 60000) throw new Error('evernook:invalid_consolidation_config');
  const ordinary = new MemoryClient(config);
  const activitySession = 'native:' + randomUUID();
  await ordinary.connect();
  try {
    const actor = (await ordinary.call<RecordValue>('status', {})).capabilities;
    const worker = (await runLocalCli(config, options.access, 'status', {}, undefined, 'consolidation')).capabilities;
    const scopes = object(worker) ? worker.scopes : undefined;
    const actorScopes = object(actor) ? actor.scopes : undefined;
    if (!object(actor) || !object(worker) || typeof actor.client_id !== 'string' || typeof worker.client_id !== 'string'
        || actor.client_id === worker.client_id || actor.entry !== config.entry
        || !Array.isArray(actorScopes) || actorScopes.some(scope => !['capture', 'recall'].includes(scope))
        || !['capture', 'recall'].every(scope => actorScopes.includes(scope))
        || !Array.isArray(scopes) || !['recall', 'maintain', 'accept'].every(scope => scopes.includes(scope))) {
      throw new Error('evernook:invalid_consolidation_identity');
    }
    await ordinary.call('host_activity', { session: activitySession, active: false });
  } catch (error) { await ordinary.close(); throw new Error('evernook:' + code(error)); }
  const lifetime = new AbortController();
  const running = new Set<Promise<RecordValue>>();
  let job: Promise<void> | undefined;
  let jobController: AbortController | undefined;
  let closed = false;
  let lastTick: RecordValue | undefined;
  let lastError: string | undefined;
  let activityHealthy = true;
  const foreground = new Set<string>();
  let activity = Promise.resolve(true);
  let repairingActivity = false;
  function syncActivity() {
    activity = activity.then(async () => {
      try {
        await ordinary.call('host_activity', { session: activitySession, active: foreground.size > 0, ttl_seconds: 30 },
          AbortSignal.any([lifetime.signal, AbortSignal.timeout(2000)]));
        activityHealthy = true;
        return true;
      } catch (error) {
        activityHealthy = false;
        if (!closed) lastError = code(error);
        return false;
      }
    });
    return activity;
  }
  function execute(access: string, operation: LocalOperation, input: RecordValue, signal?: AbortSignal) {
    const combined = signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal;
    const pending = runLocalCli(config, access, operation, input, combined, 'consolidation');
    running.add(pending);
    void pending.then(() => running.delete(pending), () => running.delete(pending));
    return pending;
  }
  function pulse(reason: 'startup' | 'idle' | 'turn_end' | 'session_end') {
    if (closed || job || foreground.size || !activityHealthy) return job;
    jobController = new AbortController();
    const controller = jobController;
    job = execute(options.access, 'scheduler-trigger', { reason }, controller.signal)
      .then(result => { lastTick = result; lastError = undefined; })
      .catch(error => { if (!closed && !(controller.signal.aborted && foreground.size)) lastError = code(error); })
      .finally(() => { job = undefined; jobController = undefined; });
    return job;
  }
  const timer = setInterval(() => {
    if (closed || repairingActivity) return;
    if (activityHealthy) { void pulse('idle'); return; }
    repairingActivity = true;
    void syncActivity().then(healthy => { if (healthy) void pulse('idle'); })
      .finally(() => { repairingActivity = false; });
  }, pollMs);
  timer.unref();
  const heartbeat = setInterval(() => { if (!closed && foreground.size) void syncActivity(); }, 10000);
  heartbeat.unref();
  void pulse('startup');
  return {
    async turnStarted(session: string) {
      if (closed) throw new Error('evernook:adapter_closed');
      foreground.add(session); jobController?.abort();
      await syncActivity();
    },
    async turnEnded(session: string) {
      if (closed) return;
      foreground.delete(session);
      await syncActivity();
      void pulse('turn_end');
    },
    async sessionEnded(session: string) {
      if (closed) return;
      foreground.delete(session);
      await syncActivity();
      if (closed) return;
      try {
        await execute(config.access, 'scheduler-session-end', { entry: config.entry, session });
        await pulse('session_end');
      } catch (error) { if (!closed) lastError = code(error); }
    },
    async status() { return execute(options.access, 'scheduler-status', {}); },
    snapshot() { return { last_tick: lastTick ? structuredClone(lastTick) : null, last_error: lastError ?? null,
      foreground: foreground.size, activity_healthy: activityHealthy, closed }; },
    async close() {
      if (closed) return;
      closed = true; foreground.clear(); clearInterval(timer); clearInterval(heartbeat); lifetime.abort(); jobController?.abort();
      await Promise.allSettled([...running]);
      await activity;
      try { await ordinary.call('host_activity', { session: activitySession, active: false }, AbortSignal.timeout(1000)); }
      catch { /* The owned lease expires if the control channel cannot be reached. */ }
      await ordinary.close();
    },
  };
}
