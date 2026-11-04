import { createHash } from 'node:crypto';
import { readFile, stat } from 'node:fs/promises';
import { parseSessionEntries, type CompactionEntry, type ExtensionContext, type SessionEntry } from '@earendil-works/pi-coding-agent';
import { MemoryClient, type Source, type WorkView } from './client.ts';
import { currentWork, sourceSnapshot } from './work-continuation.ts';

type Origin = { session: string; entry: SessionEntry; index: number };
const digest = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const body = (entry: SessionEntry) => JSON.stringify({ ...entry, parentId: null });

/** Pi retains entry IDs in forked files. Reuse their original source controls. */
export class PiWork {
  private origins = new Map<string, Map<string, Origin>>();
  private heads = new Map<string, { task: string; branch: string }>();
  private errors = new Map<string, string>();
  private connection: () => Promise<MemoryClient>;
  private entry: string;
  constructor(connection: () => Promise<MemoryClient>, entry: string) { this.connection = connection; this.entry = entry; }

  private async manifest(session: string, parent: string, current: ReadonlyMap<string, SessionEntry>, requireAll: boolean) {
    let saved;
    try { saved = await sourceSnapshot(await this.connection(), { entry: this.entry, session, branch: 'work', event: 'fork-origins-v1' }); }
    catch (error) {
      if (error instanceof Error && error.message === 'evernook:message_not_found') return null;
      throw error;
    }
    const value = JSON.parse(saved.stored.event.text) as { kind?: unknown; parent?: unknown; records?: unknown; depth?: unknown };
    if (value.kind !== 'evernook_pi_origins_v1' || value.parent !== parent || !Array.isArray(value.records)
        || typeof value.depth !== 'number' || !Number.isSafeInteger(value.depth) || value.depth < 1 || value.depth > 64
        || value.records.length > 1024 || !saved.stored.receipt.original_saved || saved.stored.event.role !== 'system'
        || saved.stored.event.completion !== 'complete') throw new Error('evernook:continuation_origin_unavailable');
    const origins = new Map<string, Origin>();
    const seen = new Set<string>();
    for (const record of value.records) {
      if (!record || typeof record.id !== 'string' || !record.id || record.id.length > 256
          || typeof record.session !== 'string' || !record.session || record.session.length > 256
          || !Number.isSafeInteger(record.index) || record.index < 0 || (record.parent !== null && typeof record.parent !== 'string')
          || typeof record.hash !== 'string' || !/^[0-9a-f]{64}$/.test(record.hash) || seen.has(record.id)) {
        throw new Error('evernook:continuation_origin_unavailable');
      }
      seen.add(record.id);
      const copied = current.get(record.id);
      if (!copied && !requireAll) continue;
      if (!copied || digest(body(copied)) !== record.hash) throw new Error('evernook:continuation_origin_unavailable');
      origins.set(record.id, { session: record.session, index: record.index, entry: { ...copied, parentId: record.parent } });
    }
    return { origins, depth: value.depth };
  }

  async reconcileOrigins(ctx: ExtensionContext) {
    const session = ctx.sessionManager.getSessionId();
    if (this.origins.has(session)) return;
    const current = new Map(ctx.sessionManager.getEntries().map(entry => [entry.id, entry]));
    const origins = new Map<string, Origin>();
    const parent = ctx.sessionManager.getHeader()?.parentSession;
    const memory = await this.connection();
    const manifestSource: Source = { entry: this.entry, session, branch: 'work', event: 'fork-origins-v1' };
    if (parent) {
      const verified = await this.manifest(session, parent, current, true);
      if (verified) {
        this.origins.set(session, verified.origins);
        return;
      }
    }
    let depth = 0;
    if (parent) {
      if ((await stat(parent)).size > 64 * 1024 * 1024) throw new Error('evernook:continuation_ancestry_limit');
      const entries = parseSessionEntries(await readFile(parent, 'utf8'));
      const header = entries.find(item => item.type === 'session');
      if (!header || header.type !== 'session') throw new Error('evernook:continuation_origin_unavailable');
      const inherited = header.parentSession ? await this.manifest(header.id, header.parentSession, current, false) : null;
      if (header.parentSession && !inherited) throw new Error('evernook:continuation_origin_unavailable');
      depth = (inherited?.depth ?? 0) + 1;
      if (depth > 64) throw new Error('evernook:continuation_ancestry_limit');
      for (const [id, origin] of inherited?.origins ?? []) origins.set(id, origin);
      const records = entries.filter((item): item is SessionEntry => item.type !== 'session');
      for (const [index, entry] of records.entries()) {
        const copied = current.get(entry.id);
        if (copied && body(copied) === body(entry) && !origins.has(entry.id)) origins.set(entry.id, { session: header.id, entry, index });
      }
    }
    if (parent) {
      for (const record of current.values()) {
        if (!origins.has(record.id) && !['model_change', 'thinking_level_change', 'session_info', 'usage', 'label'].includes(record.type)) {
          throw new Error('evernook:continuation_origin_unavailable');
        }
      }
      if (origins.size > 1024) throw new Error('evernook:continuation_ancestry_limit');
      await memory.remember({ source: manifestSource, role: 'system', sequence: 0, occurred_at: null, completion: 'complete',
        text: JSON.stringify({ kind: 'evernook_pi_origins_v1', parent, depth,
          records: [...origins].map(([id, origin]) => ({ id, session: origin.session, index: origin.index,
            parent: origin.entry.parentId, hash: digest(body(origin.entry)) })) }) });
      await sourceSnapshot(memory, manifestSource);
    }
    this.origins.set(session, origins);
  }

  origin(ctx: ExtensionContext, record: SessionEntry, index: number): Origin {
    return this.origins.get(ctx.sessionManager.getSessionId())?.get(record.id)
      ?? { session: ctx.sessionManager.getSessionId(), entry: record, index };
  }
  source(ctx: ExtensionContext, record: SessionEntry, index: number, kind: string): Source {
    const origin = this.origin(ctx, record, index);
    return { entry: this.entry, session: origin.session, branch: 'parent:' + (origin.entry.parentId ?? 'root'), event: `${kind}:${record.id}` };
  }

  async unavailableHistory(ctx: ExtensionContext): Promise<string[]> {
    const memory = await this.connection();
    const all = ctx.sessionManager.getEntries();
    const unavailable: string[] = [];
    for (const projected of ctx.sessionManager.buildSessionProjection().entries) {
      const record = projected.sourceEntry;
      if (record.type !== 'message' || (record.message.role !== 'user' && record.message.role !== 'assistant'
          && record.message.role !== 'toolResult')) continue;
      const index = all.findIndex(item => item.id === record.id);
      try {
        if (record.message.role === 'toolResult') {
          const call = record.message.toolCallId;
          await sourceSnapshot(memory, { entry: this.entry, session: this.origin(ctx, record, index).session,
            branch: 'tool:' + call, event: 'tool-result:' + call });
        }
        if (record.message.role === 'assistant') {
          const link = await sourceSnapshot(memory, this.source(ctx, record, index, 'stream-link'));
          const value = JSON.parse(link.stored.event.text) as { mode: string; source: Source };
          if (value.mode === 'durable_text') {
            const stream = await memory.readStream(value.source);
            if (!stream.final_message_id) throw new Error('evernook:continuation_stream_incomplete');
            await sourceSnapshot(memory, (await memory.read(stream.final_message_id)).event.source);
            continue;
          }
          await sourceSnapshot(memory, value.source);
        }
        const saved = await sourceSnapshot(memory, this.source(ctx, record, index, 'native'));
        if (!saved.stored.receipt.original_saved || saved.stored.event.completion !== 'complete') throw new Error('evernook:continuation_original_required');
        continue;
      } catch {
        const text = (content: unknown) => typeof content === 'string' ? content : Array.isArray(content)
          ? content.filter(item => item.type === 'text').map(item => item.text).join('') : '';
        unavailable.push(text(record.message.content));
        for (const message of projected.messages) if ('content' in message) unavailable.push(text(message.content));
      }
    }
    return unavailable;
  }

  async prepare(ctx: ExtensionContext, consumed: WorkView | null) {
    const memory = await this.connection();
    const records = ctx.sessionManager.buildSessionProjection().entries
      .filter(item => item.messages.length || item.sourceEntry.type === 'context_edit').map(item => item.sourceEntry);
    if (records.length > 256) throw new Error('evernook:continuation_source_limit');
    const dependencies: [string, number, number][] = [];
    const all = ctx.sessionManager.getEntries();
    for (const record of records) {
      const index = all.findIndex(item => item.id === record.id);
      dependencies.push((await sourceSnapshot(memory, this.source(ctx, record, index, 'audit'))).reference);
      if (record.type !== 'message') continue;
      if (['user', 'assistant', 'toolResult'].includes(record.message.role)) {
        if (record.message.role !== 'assistant') {
          dependencies.push((await sourceSnapshot(memory, this.source(ctx, record, index, 'native'))).reference);
          if (record.message.role === 'toolResult') {
            const call = record.message.toolCallId;
            dependencies.push((await sourceSnapshot(memory, { entry: this.entry, session: this.origin(ctx, record, index).session,
              branch: 'tool:' + call, event: 'tool-result:' + call })).reference);
          }
        } else {
          const link = await sourceSnapshot(memory, this.source(ctx, record, index, 'stream-link'));
          dependencies.push(link.reference);
          const value = JSON.parse(link.stored.event.text) as { mode: string; source: Source };
          if (value.source.entry !== this.entry || value.source.session !== this.origin(ctx, record, index).session) {
            throw new Error('evernook:continuation_stream_link_invalid');
          }
          if (value.mode === 'durable_text') {
            const stream = await memory.readStream(value.source);
            if (!stream.final_message_id) throw new Error('evernook:continuation_stream_incomplete');
            const original = await memory.read(stream.final_message_id);
            dependencies.push((await sourceSnapshot(memory, original.event.source)).reference);
          } else if (value.mode === 'buffered') {
            dependencies.push((await sourceSnapshot(memory, value.source)).reference);
            dependencies.push((await sourceSnapshot(memory, this.source(ctx, record, index, 'native'))).reference);
          } else throw new Error('evernook:continuation_stream_link_invalid');
        }
      }
    }
    const source: Source = { entry: this.entry, session: ctx.sessionManager.getSessionId(), branch: 'work',
      event: 'work-input:' + digest([ctx.sessionManager.getLeafId(), consumed?.checkpoint_id ?? null]) };
    await memory.remember({ source, role: 'system', sequence: 0, occurred_at: null, completion: 'complete',
      text: JSON.stringify({ kind: 'evernook_pi_work_input_v1', dependencies, consumed: consumed ? [consumed.checkpoint_id] : [] }) });
    return source;
  }

  async capture(ctx: ExtensionContext) {
    const session = ctx.sessionManager.getSessionId();
    const branch = ctx.sessionManager.getBranch();
    const goal = branch.find(record => record.type === 'message' && record.message.role === 'user');
    const compact = branch.findLast((record): record is CompactionEntry => record.type === 'compaction');
    this.heads.delete(session);
    if (!goal || !compact) { this.errors.delete(session); return; }
    const task = 'pi-work:' + digest(goal.id);
    const origin = this.origin(ctx, compact, ctx.sessionManager.getEntries().findIndex(record => record.id === compact.id));
    const target = 'pi-branch:' + digest([origin.session, compact.id]);
    this.heads.set(session, { task, branch: target });
    const memory = await this.connection();
    try {
      if (await currentWork(memory, task, target)) { this.errors.delete(session); return; }
      const details = compact.details as { evernook_work_v1?: { input: Source; outputs: Source[]; mode: string } } | undefined;
      const input = details?.evernook_work_v1?.input;
      if (!input || input.entry !== this.entry || input.session !== origin.session || input.branch !== 'work'
          || !/^work-input:[0-9a-f]{64}$/.test(input.event)) throw new Error('evernook:continuation_input_untracked');
      const proof = await sourceSnapshot(memory, input);
      const data = JSON.parse(proof.stored.event.text) as { kind: string; dependencies: [string, number, number][]; consumed: string[] };
      if (data.kind !== 'evernook_pi_work_input_v1') throw new Error('evernook:continuation_input_untracked');
      const outputs = details!.evernook_work_v1!;
      if (!Array.isArray(outputs.outputs) || !outputs.outputs.length || outputs.outputs.length > 8
          || !['buffered', 'durable_text'].includes(outputs.mode)) throw new Error('evernook:continuation_stream_link_invalid');
      for (const source of outputs.outputs) {
        if (source.entry !== this.entry || source.session !== origin.session) throw new Error('evernook:continuation_stream_link_invalid');
        if (outputs.mode === 'durable_text') {
          const stream = await memory.readStream(source);
          if (!stream.final_message_id) throw new Error('evernook:continuation_stream_incomplete');
          const stored = await memory.read(stream.final_message_id);
          data.dependencies.push((await sourceSnapshot(memory, stored.event.source)).reference);
        } else data.dependencies.push((await sourceSnapshot(memory, source)).reference);
      }
      const all = ctx.sessionManager.getEntries();
      const goalSource = await sourceSnapshot(memory, this.source(ctx, goal, all.findIndex(record => record.id === goal.id), 'native'));
      const points = Array.from(compact.summary);
      if (!points.length || points.length > 16384) throw new Error('evernook:continuation_summary_limit');
      const source = this.source(ctx, compact, all.findIndex(record => record.id === compact.id), 'work-summary');
      await memory.remember({ source, role: 'system', text: compact.summary, sequence: origin.index,
        occurred_at: compact.timestamp, completion: 'complete' });
      const summary = await sourceSnapshot(memory, source);
      await memory.call('continuation_save', { request_id: 'pi-compact:' + digest([this.entry, origin.session, compact.id]), draft: {
        task_id: task, branch_id: target, expected_revision: 0, anchor_id: summary.reference[0], goal: goalSource.span(),
        progress: [summary.span()], pending: [], state: 'unknown', goal_role: 'initial_user_request',
        expires_at: new Date(Date.parse(compact.timestamp) + 7 * 86400000).toISOString(),
        dependencies: [proof.reference, ...data.dependencies], consumed_checkpoints: data.consumed } });
      this.errors.delete(session);
    } catch (error) {
      this.errors.set(session, error instanceof Error && /^evernook:[a-z_]+$/.test(error.message)
        ? error.message.slice(9) : 'continuation_unavailable');
    }
  }
  async current(session: string): Promise<WorkView | null> {
    if (this.errors.has(session)) return null;
    const head = this.heads.get(session);
    return head ? currentWork(await this.connection(), head.task, head.branch) : null;
  }
  error(session: string) { return this.errors.get(session); }
}
