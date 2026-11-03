import { runLocalCli as run, type MaintenanceOperation as Operation } from './local-cli.ts';
import { randomUUID } from 'node:crypto';
import { isAbsolute } from 'node:path';
import { MemoryClient, type MemoryConfig } from './client.ts';

type RecordValue = Record<string, unknown>;
type Apply = (confirmationId: string, signal?: AbortSignal) => Promise<RecordValue>;
export type MaintenanceProposal = { kind: 'pin' | 'suppress'; message_id: string }
  | { kind: 'correct_source'; message_id: string; replacement_id: string }
  | { kind: 'purge'; source_ids: string[] }
  | { kind: 'profile'; tag_id: string; action: 'revise' | 'confirm' | 'suppress'; draft?: RecordValue }
  | { kind: 'fact_correction'; subject_id: string; predicate: string; candidate_id: string; run_id: string };
export interface MaintenanceReview {
  proposal_id: string; session_id: string; operation: MaintenanceProposal; confirm_command: string;
  created_at: number; sources: { message_id: string; source: unknown; text: string; total: number | null; truncated: boolean; state: string }[];
  residuals?: string[]; purge_preview?: RecordValue;
  previous?: RecordValue;
  candidate?: RecordValue;
}
export interface MaintenanceOutcome {
  proposal_id: string; state: 'pending' | 'executing' | 'completed' | 'failed' | 'rejected';
  confirmation_id?: string; result?: RecordValue; error_code?: string;
}
export interface MaintenanceConfig {
  access: string;
  onReview: (review: MaintenanceReview, signal: AbortSignal) => Promise<void>;
  onResult: (result: MaintenanceOutcome) => Promise<void>;
}
function object(value: unknown): value is RecordValue { return value !== null && typeof value === 'object' && !Array.isArray(value); }

/** A proposal never authorizes itself. Only a subsequently saved native user command can confirm it. */
export async function createMaintenance(config: MemoryConfig, maintenance: MaintenanceConfig) {
  config = { ...config };
  maintenance = { ...maintenance };
  if (!isAbsolute(maintenance.access) || maintenance.access === config.access
      || typeof maintenance.onReview !== 'function' || typeof maintenance.onResult !== 'function') throw new Error('evernook:invalid_maintenance_config');
  const ordinary = new MemoryClient(config);
  await ordinary.connect();
  try {
    const actor = await ordinary.call<RecordValue>('status', {});
    const actorCapabilities = actor.capabilities;
    const privileged = await run(config, maintenance.access, 'status', {});
    const capabilities = privileged.capabilities;
    const scopes = object(capabilities) ? capabilities.scopes : undefined;
    if (!object(actorCapabilities) || typeof actorCapabilities.client_id !== 'string' || !Array.isArray(actorCapabilities.scopes)
        || !object(capabilities) || typeof capabilities.client_id !== 'string'
        || capabilities.client_id === actorCapabilities.client_id || !Array.isArray(scopes)
        || !['recall', 'maintain', 'accept', 'admin'].every(scope => scopes.includes(scope))
        || actorCapabilities.scopes.some(scope => !['capture', 'recall'].includes(scope))) throw new Error('evernook:invalid_maintenance_identity');
  } catch (error) { await ordinary.close(); throw error; }
  const entries = new Map<string, { review: MaintenanceReview;
    apply?: Apply; outcome: MaintenanceOutcome; lastAttemptId?: string }>();
  const lifetime = new AbortController();
  const running = new Set<Promise<RecordValue>>();
  function execute(operation: Operation, input: RecordValue, signal?: AbortSignal) {
    const combined = signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal;
    const pending = run(config, maintenance.access, operation, input, combined);
    running.add(pending);
    void pending.then(() => running.delete(pending), () => running.delete(pending));
    return pending;
  }
  let closed = false;
  async function confirm(sessionId: string, messageId: string, signal?: AbortSignal) {
    if (closed) throw new Error('evernook:adapter_closed');
    const message = await ordinary.read(messageId, signal);
    if (!message.receipt.original_saved || message.event.source.session !== sessionId) return;
    const match = /^\/memory confirm ([a-f0-9-]{36})$/.exec(message.event.text);
    if (!match) return;
    const entry = entries.get(match[1]);
    if (!entry || entry.review.session_id !== sessionId
        || message.event.role !== 'user' || message.event.completion !== 'complete'
        || !Number.isFinite(Date.parse(message.receipt.received_at))
        || Date.parse(message.receipt.received_at) < entry.review.created_at
        || entry.outcome.state === 'completed' || entry.outcome.state === 'rejected' || entry.outcome.state === 'executing'
        || entry.lastAttemptId === messageId) return;
    entry.lastAttemptId = messageId;
    const confirmationId = entry.outcome.confirmation_id ?? messageId;
    const apply = entry.apply;
    if (!apply) return;
    entry.outcome = { proposal_id: entry.review.proposal_id, state: 'executing', confirmation_id: confirmationId };
    try {
      signal?.throwIfAborted();
      const saved = await apply(confirmationId, signal);
      entry.outcome = { ...entry.outcome, state: 'completed', result: saved };
      entry.apply = undefined;
      entries.delete(entry.review.proposal_id); entries.set(entry.review.proposal_id, entry);
      const completed = [...entries].filter(([, value]) => ['completed', 'rejected'].includes(value.outcome.state));
      for (const [id] of completed.slice(0, Math.max(0, completed.length - 64))) entries.delete(id);
    } catch (error) {
      const code = error instanceof Error ? /^evernook:([a-z_]{1,80})$/.exec(error.message)?.[1] : undefined;
      entry.outcome = { ...entry.outcome, state: 'failed', error_code: code ?? 'maintenance_unavailable' };
    }
    if (!closed) {
      try { await maintenance.onResult(structuredClone(entry.outcome)); }
      catch { throw new Error('evernook:maintenance_receipt_unavailable'); }
    }
    return;
  }
  async function publishReview(id: string, sessionId: string, operation: MaintenanceProposal,
      sources: MaintenanceReview['sources'], apply: Apply,
      details: Partial<Pick<MaintenanceReview, 'previous' | 'candidate' | 'purge_preview' | 'residuals'>>,
      signal: AbortSignal) {
    const review: MaintenanceReview = { proposal_id: id, session_id: sessionId, operation: structuredClone(operation),
      confirm_command: '/memory confirm ' + id, created_at: Date.now(), sources, ...details };
    signal.throwIfAborted();
    try { await maintenance.onReview(structuredClone(review), signal); }
    catch { throw new Error(signal.aborted ? 'evernook:operation_canceled' : 'evernook:maintenance_review_unavailable'); }
    signal.throwIfAborted();
    entries.set(id, { review, apply, outcome: { proposal_id: id, state: 'pending' } });
    return structuredClone(review);
  }
  async function prepareProposal(sessionId: string, proposal: MaintenanceProposal, signal: AbortSignal): Promise<MaintenanceReview> {
      if (closed || !object(proposal)) throw new Error('evernook:invalid_maintenance_proposal');
      signal.throwIfAborted();
      proposal = structuredClone(proposal);
      const keys = Object.keys(proposal).sort().join(',');
      if (proposal.kind === 'fact_correction') {
        if (keys !== 'candidate_id,kind,predicate,run_id,subject_id'
            || [proposal.subject_id, proposal.predicate, proposal.candidate_id, proposal.run_id].some(value => typeof value !== 'string')) throw new Error('evernook:invalid_maintenance_proposal');
        const previous = await execute('fact-query', { subject_id: proposal.subject_id, predicate: proposal.predicate }, signal);
        const fact = previous.fact;
        if (!object(fact) || typeof fact.fact_id !== 'string' || !Number.isSafeInteger(previous.revision)
            || previous.clarification_required === true) throw new Error('evernook:fact_current_review_required');
        const run = await execute('candidate-list', { run_id: proposal.run_id }, signal);
        const candidate: unknown = Array.isArray(run.candidates)
          ? run.candidates.find(value => object(value) && value.candidate_id === proposal.candidate_id) : undefined;
        if (!object(candidate) || candidate.status !== 'candidate' || !object(candidate.draft)
            || !Array.isArray(candidate.draft.sources)) throw new Error('evernook:candidate_not_found');
        const sources: MaintenanceReview['sources'] = [];
        for (const span of candidate.draft.sources) {
          if (!object(span) || typeof span.message_id !== 'string') throw new Error('evernook:invalid_maintenance_proposal');
          const stored = await ordinary.read(span.message_id, signal);
          if (!stored.receipt.original_saved) throw new Error('evernook:source_original_unavailable');
          const points = Array.from(stored.event.text);
          sources.push({ message_id: span.message_id, source: stored.event.source, text: points.slice(0, 2048).join(''),
            total: points.length, truncated: points.length > 2048, state: 'active' });
        }
        const id = randomUUID();
        const selected = proposal;
        const apply: Apply = (confirmationId, signal) => execute('fact-decide', { request_id: id, subject_id: selected.subject_id,
          predicate: selected.predicate, candidate_id: selected.candidate_id, confirmation_id: confirmationId,
          expected_revision: previous.revision, kind: 'correction', replaces_id: fact.fact_id,
          valid_from: fact.valid_from, valid_until: fact.valid_until, occurred_at: fact.occurred_at, category: fact.category }, signal);
        return publishReview(id, sessionId, selected, sources, apply, { previous, candidate }, signal);
      }
      if (proposal.kind === 'profile') {
        if (typeof proposal.tag_id !== 'string' || !['revise', 'confirm', 'suppress'].includes(proposal.action)
            || keys !== (proposal.action === 'revise' ? 'action,draft,kind,tag_id' : 'action,kind,tag_id')) throw new Error('evernook:invalid_maintenance_proposal');
        if (proposal.action === 'revise' && (!object(proposal.draft) || !Array.isArray(proposal.draft.sources)
            || proposal.draft.sources.length < 1 || proposal.draft.sources.length > 16)) throw new Error('evernook:invalid_maintenance_proposal');
        const id = randomUUID();
        const previous = await execute('profile-read', { tag_id: proposal.tag_id }, signal);
        if (!object(previous.record) || !Number.isSafeInteger(previous.revision)) throw new Error('evernook:profile_not_found');
        const draft = proposal.action === 'revise' ? proposal.draft : previous.record.draft;
        if (!object(draft) || !Array.isArray(draft.sources)) throw new Error('evernook:invalid_maintenance_proposal');
        const sources: MaintenanceReview['sources'] = [];
        for (const span of draft.sources) {
          if (!object(span) || typeof span.message_id !== 'string') throw new Error('evernook:invalid_maintenance_proposal');
          try {
            const stored = await ordinary.read(span.message_id, signal);
            const points = Array.from(stored.event.text);
            sources.push({ message_id: span.message_id, source: stored.event.source, text: points.slice(0, 2048).join(''),
              total: points.length, truncated: points.length > 2048, state: 'active' });
          } catch (error) {
            if (proposal.action === 'revise' || !(error instanceof Error) || error.message !== 'evernook:message_not_found') throw error;
            sources.push({ message_id: span.message_id, source: null, text: '', total: null, truncated: false, state: 'unavailable' });
          }
        }
        const selected = proposal;
        const apply: Apply = (confirmationId, signal) => execute('profile-decide', { request_id: id, tag_id: selected.tag_id,
          expected_revision: previous.revision, action: selected.action,
          ...(selected.action === 'revise' ? { draft: selected.draft } : { confirmation_id: confirmationId }) }, signal);
        return publishReview(id, sessionId, selected, sources, apply, { previous }, signal);
      }
      if (proposal.kind === 'purge') {
        if (keys !== 'kind,source_ids' || !Array.isArray(proposal.source_ids) || proposal.source_ids.length < 1
            || proposal.source_ids.length > 16 || proposal.source_ids.some(id => typeof id !== 'string')) throw new Error('evernook:invalid_maintenance_proposal');
      } else if (proposal.kind === 'correct_source') {
        if (keys !== 'kind,message_id,replacement_id' || typeof proposal.message_id !== 'string'
            || typeof proposal.replacement_id !== 'string' || proposal.message_id === proposal.replacement_id) throw new Error('evernook:invalid_maintenance_proposal');
      } else if (keys !== 'kind,message_id' || !['pin', 'suppress'].includes(proposal.kind)
          || typeof proposal.message_id !== 'string') throw new Error('evernook:invalid_maintenance_proposal');
      const id = randomUUID();
      let preview: RecordValue | undefined;
      let operation: MaintenanceProposal = structuredClone(proposal);
      if (proposal.kind === 'purge') {
        preview = await execute('purge-preview', { request_id: id, source_ids: proposal.source_ids }, signal);
        if (!Array.isArray(preview.source_ids) || preview.source_ids.some(item => typeof item !== 'string')
            || !Array.isArray(preview.expected_versions) || !Array.isArray(preview.residuals)
            || preview.residuals.some(item => typeof item !== 'string')) throw new Error('evernook:invalid_maintenance_result');
        operation = { kind: 'purge', source_ids: [...preview.source_ids] };
      }
      const sourceIds = operation.kind === 'purge' ? operation.source_ids : operation.kind === 'correct_source'
        ? [operation.message_id, operation.replacement_id] : [operation.message_id];
      const sources: MaintenanceReview['sources'] = [];
      let text = '';
      let contentVersion = 0;
      let stateVersion = 0;
      for (const [index, messageId] of sourceIds.entries()) {
        let stored: Awaited<ReturnType<MemoryClient['read']>> | undefined;
        try { stored = await ordinary.read(messageId, signal); }
        catch (error) {
          if (operation.kind !== 'purge' || !(error instanceof Error) || error.message !== 'evernook:message_not_found') throw error;
        }
        const state = await ordinary.call<{ status: string; version: number }>('state', { message_id: messageId }, signal);
        if (operation.kind !== 'purge' && state.status !== 'active') throw new Error('evernook:source_changed');
        if (preview && Array.isArray(preview.expected_versions) && state.version !== preview.expected_versions[index]) throw new Error('evernook:source_changed');
        if (operation.kind === 'pin' && !stored?.receipt.original_saved) throw new Error('evernook:source_original_unavailable');
        if (operation.kind === 'correct_source' && index === 1 && (!stored?.receipt.original_saved
            || stored.event.role !== 'user' || stored.event.completion !== 'complete')) throw new Error('evernook:insufficient_replacement_evidence');
        const points = stored ? Array.from(stored.event.text) : [];
        sources.push({ message_id: messageId, source: stored?.event.source ?? null, text: points.slice(0, 2048).join(''),
          total: stored ? points.length : null, truncated: points.length > 2048, state: state.status });
        if (index === 0) { text = stored?.event.text ?? ''; contentVersion = stored?.receipt.content_version ?? 0; stateVersion = state.version; }
      }
      const apply = operation.kind === 'purge'
        ? (confirmationId: string, signal?: AbortSignal) => execute('purge', { request_id: id, confirmation_id: confirmationId }, signal)
        : operation.kind === 'pin'
        ? (_confirmationId: string, signal?: AbortSignal) => execute('mark-important', { message_id: sourceIds[0], representation: 'original',
          content_version: contentVersion, scope: 'shared', start: null, end: null, expected_text: text }, signal)
        : (confirmationId: string, signal?: AbortSignal) => execute('change', { request_id: id, target_id: sourceIds[0],
          expected_version: stateVersion, action: operation.kind === 'correct_source' ? 'correct' : 'suppress',
          evidence_id: operation.kind === 'correct_source' ? operation.replacement_id : confirmationId,
          reason: 'Native user confirmed proposal ' + id + ' via ' + confirmationId }, signal);
      return publishReview(id, sessionId, operation, sources, apply, preview
        ? { purge_preview: structuredClone(preview), residuals: [...preview.residuals as string[]] } : {}, signal);
  }
  let preparing = 0;
  const service = {
    confirm,
    async reject(proposalId: string) {
      if (closed) throw new Error('evernook:adapter_closed');
      const entry = entries.get(proposalId);
      if (!entry || entry.outcome.state !== 'pending') throw new Error('evernook:proposal_not_pending');
      entry.apply = undefined; entry.outcome = { proposal_id: proposalId, state: 'rejected' };
      const terminal = [...entries].filter(([, value]) => ['completed', 'rejected'].includes(value.outcome.state));
      for (const [id] of terminal.slice(0, Math.max(0, terminal.length - 64))) entries.delete(id);
      try { await maintenance.onResult(structuredClone(entry.outcome)); }
      catch { throw new Error('evernook:maintenance_receipt_unavailable'); }
    },
    async propose(sessionId: string, proposal: MaintenanceProposal, signal?: AbortSignal): Promise<MaintenanceReview> {
      if (preparing + [...entries.values()].filter(entry => !['completed', 'rejected'].includes(entry.outcome.state)).length >= 64) throw new Error('evernook:maintenance_queue_full');
      preparing++;
      try { return await prepareProposal(sessionId, proposal, signal ? AbortSignal.any([signal, lifetime.signal]) : lifetime.signal); }
      finally { preparing--; }
    },
    status(proposalId: string) { const value = entries.get(proposalId)?.outcome; return value ? structuredClone(value) : undefined; },
    readImportant(important_id: string, cursor = 0, limit = 2048) {
      return ordinary.call<{ text: string; next_cursor: number | null; remaining: number }>('read_important', { important_id, cursor, limit });
    },
    async readSource(message_id: string, cursor = 0, limit = 2048) {
      if (!Number.isSafeInteger(cursor) || cursor < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 4096) throw new Error('evernook:invalid_source_page');
      const stored = await ordinary.read(message_id);
      const points = Array.from(stored.event.text);
      if (cursor > points.length) throw new Error('evernook:invalid_source_page');
      const end = Math.min(points.length, cursor + limit);
      return { message_id, source: stored.event.source, representation: stored.receipt.representation,
        content_version: stored.receipt.content_version, text: points.slice(cursor, end).join(''), start: cursor, end,
        total: points.length, next_cursor: end < points.length ? end : null };
    },
    async close() { if (closed) return; closed = true; lifetime.abort();
      await Promise.allSettled([...running]); await ordinary.close(); },
  };
  return service;
}
