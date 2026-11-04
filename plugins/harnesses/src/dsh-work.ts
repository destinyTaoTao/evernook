import { createHash } from 'node:crypto';
import type { SessionEvent, SessionHeader } from '@deepseek-ai/dsh-session';
import type {} from '@deepseek-ai/dsh-compaction';
import type { RequestMessage } from '@deepseek-ai/dsh-llm';
import { MemoryClient, type Source, type WorkSpan, type WorkView } from './client.ts';
import { currentWork, sourceSnapshot, summaryParts } from './work-continuation.ts';

function textOf(content: readonly { type: string; text?: string }[]) {
  return content.filter(block => block.type === 'text').map(block => block.text ?? '').join('');
}

/** Native event identities bind summaries to their actual source controls, including reliable streams. */
export class DshWork {
  private steps = new Map<string, { seq: number; attempt: number }>();
  private keys = new Map<string, string>();
  private errors = new Map<string, string>();
  private events = new Map<string, Map<number, SessionEvent>>();
  private compactions = new Map<string, { id: string; seq: number; time: number }>();
  private client: MemoryClient;
  private entry: string;
  constructor(client: MemoryClient, entry: string) { this.client = client; this.entry = entry; }

  observe(sessionId: string, record: SessionEvent) {
    this.events.get(sessionId)?.set(record.seq, record);
    if (record.type === 'compaction/start') this.compactions.set(sessionId, { id: String(record.data.compactionId), seq: record.seq, time: record.time });
    if (record.type === 'compaction/end') this.compactions.delete(sessionId);
    if (record.type === 'step/start') this.steps.set(sessionId, { seq: record.seq, attempt: 0 });
    if (record.type === 'assistant/message' || record.type === 'assistant/attempt') {
      const step = this.steps.get(sessionId);
      if (step) step.attempt++;
    }
  }

  async link(sessionId: string, source: Source, mode: 'buffered' | 'durable_text') {
    const step = this.steps.get(sessionId);
    if (!step) throw new Error('evernook:native_step_unavailable');
    await this.client.remember({ source: this.source(sessionId, `native-stream:${step.seq}:${step.attempt}`), role: 'system',
      sequence: step.seq, occurred_at: null, completion: 'complete',
      text: JSON.stringify({ kind: 'evernook_native_stream_link_v1', mode, source }) });
  }

  private source(session: string, event: string): Source { return { entry: this.entry, session, branch: session, event }; }

  async consume(sessionId: string, messages: RequestMessage[], continuation: WorkView | null) {
    const compaction = this.compactions.get(sessionId);
    if (!compaction) return;
    const ids = new Set(messages.flatMap(message => 'id' in message && typeof message.id === 'string' ? [String(message.id)] : []));
    const seqs: number[] = [];
    for (const record of this.events.get(sessionId)?.values() ?? []) {
      const message = record.type === 'user/message' ? record.data
        : ['assistant/message', 'system/message', 'developer/message', 'tool/result'].includes(record.type)
          && 'message' in record.data ? record.data.message : undefined;
      if (message && typeof message === 'object' && 'id' in message && ids.delete(String(message.id))) seqs.push(record.seq);
    }
    if (ids.size) throw new Error('evernook:continuation_input_untracked');
    const consumed = continuation && (continuation.goal.text || continuation.progress.length || continuation.pending.length)
      ? [continuation.checkpoint_id] : [];
    await this.client.remember({ source: this.source(sessionId, 'work-input:' + compaction.id), role: 'system',
      sequence: compaction.seq, occurred_at: new Date(compaction.time).toISOString(), completion: 'complete',
      text: JSON.stringify({ kind: 'evernook_compaction_input_v1', event_seqs: seqs, consumed_checkpoints: consumed }) });
  }

  async capture(header: SessionHeader, events: readonly SessionEvent[], origin: (seq: number) => string) {
    const sessionId = String(header.id);
    this.events.set(sessionId, new Map(events.map(record => [Number(record.seq), record])));
    const open = events.findLast(record => record.type === 'compaction/start');
    if (open?.type === 'compaction/start' && !events.some(record => record.type === 'compaction/end'
        && record.data.compactionId === open.data.compactionId)) {
      this.compactions.set(sessionId, { id: String(open.data.compactionId), seq: open.seq, time: open.time });
    }
    const goalRecord = events.find(record => record.type === 'user/message' && record.data.source.kind === 'user');
    if (!goalRecord || goalRecord.type !== 'user/message') return;
    const taskId = 'dsh-work:' + createHash('sha256').update(String(goalRecord.data.id)).digest('hex');
    this.keys.set(sessionId, taskId);
    const summary = events.findLast(record => record.type === 'compaction/summary'
      && events.some(end => end.type === 'compaction/end' && end.data.compactionId === record.data.compactionId && !end.data.error));
    if (!summary || summary.type !== 'compaction/summary') return;
    const requestId = 'native-compact:' + createHash('sha256')
      .update(JSON.stringify([this.entry, sessionId, summary.data.compactionId])).digest('hex');
    try {
      const previous = await currentWork(this.client, taskId, sessionId);
      if (previous?.request_id === requestId) { this.errors.delete(sessionId); return; }
      const consumption = await sourceSnapshot(this.client, this.source(origin(summary.seq), 'work-input:' + summary.data.compactionId));
      const input = JSON.parse(consumption.stored.event.text) as { event_seqs: number[]; consumed_checkpoints: string[] };
      if (!Array.isArray(input.event_seqs) || !Array.isArray(input.consumed_checkpoints)) throw new Error('evernook:continuation_input_untracked');
      const bySeq = new Map(events.map(record => [Number(record.seq), record]));
      const selected = new Set<number>();
      const expand = (seq: number) => {
        if (selected.has(seq)) return;
        if (selected.size >= 512) throw new Error('evernook:continuation_source_limit');
        selected.add(seq);
        const record = bySeq.get(seq);
        if (!record) throw new Error('evernook:continuation_source_unavailable');
        if (record.type === 'user/message' && record.data.source.kind === 'compact-checkpoint') {
          const id = record.data.source.compactionId;
          const prior = events.find(item => item.type === 'compaction/summary' && item.data.compactionId === id);
          if (!prior || prior.type !== 'compaction/summary') throw new Error('evernook:continuation_source_unavailable');
          for (const parent of prior.data.shadowedSeqs) expand(parent);
        }
      };
      for (const seq of summary.data.shadowedSeqs) expand(seq);
      for (const seq of input.event_seqs) expand(seq);
      const sources = new Map<string, Source>();
      const add = (source: Source) => sources.set(JSON.stringify(source), source);
      const inputs = new Map<string, Source>();
      const nativeStreams = new Map<number, Source>();
      let step = { seq: 0, attempt: 0 };
      for (const record of events) {
        if (record.type === 'step/start') step = { seq: record.seq, attempt: 0 };
        if (record.type === 'assistant/message' || record.type === 'assistant/attempt') {
          nativeStreams.set(record.seq, this.source(origin(record.seq), `native-stream:${step.seq}:${step.attempt++}`));
        }
        if (record.type === 'agent/inbox/spliced') {
          for (const [index, message] of record.data.inserted.entries()) if (message.source.kind === 'user') {
            inputs.set(String(message.id), this.source(origin(record.seq), `input:${record.seq}:${index}`));
          }
        }
        if (record.type === 'user/message' && record.data.source.kind === 'user' && !inputs.has(String(record.data.id))) {
          inputs.set(String(record.data.id), this.source(origin(record.seq), `input:${record.seq}`));
        }
      }
      for (const seq of selected) {
        const record = bySeq.get(seq)!;
        add(this.source(origin(seq), `event:${seq}`));
        if (record.type === 'user/message' && record.data.source.kind === 'user') add(inputs.get(String(record.data.id))!);
        if (record.type === 'tool/result') add(this.source(origin(seq), `tool:${seq}`));
        if (record.type === 'assistant/message') {
          const linkSource = nativeStreams.get(seq)!;
          add(linkSource);
          const link = JSON.parse((await sourceSnapshot(this.client, linkSource)).stored.event.text) as { mode: string; source: Source };
          if (!['buffered', 'durable_text'].includes(link.mode) || link.source.entry !== this.entry || link.source.session !== origin(seq)) {
            throw new Error('evernook:continuation_stream_link_invalid');
          }
          if (link.mode === 'buffered') {
            add(link.source);
            add(this.source(origin(seq), `assistant:${seq}`));
          } else {
            const stream = await this.client.readStream(link.source);
            if (!stream.final_message_id) throw new Error('evernook:continuation_stream_incomplete');
            const stored = await this.client.read(stream.final_message_id);
            add(stored.event.source);
          }
        }
      }
      if (sources.size > 1024) throw new Error('evernook:continuation_source_limit');
      const dependencies = [];
      dependencies.push(consumption.reference);
      for (const source of sources.values()) dependencies.push((await sourceSnapshot(this.client, source)).reference);
      const goalSource = inputs.get(String(goalRecord.data.id));
      if (!goalSource) throw new Error('evernook:continuation_goal_unavailable');
      const goal = (await sourceSnapshot(this.client, goalSource)).span();
      const summaryText = textOf(summary.data.summary);
      if (!summaryText.trim() || Array.from(summaryText).length > 16384) throw new Error('evernook:continuation_summary_limit');
      const source = this.source(origin(summary.seq), 'work-summary:' + summary.data.compactionId);
      await this.client.remember({ source, role: 'system', sequence: summary.seq,
        occurred_at: new Date(summary.time).toISOString(), completion: 'complete', text: summaryText });
      const snapshot = await sourceSnapshot(this.client, source);
      const parts = summaryParts(summaryText, snapshot.span());
      const state = previous && ['in_progress', 'completed', 'abandoned'].includes(previous.state) ? previous.state : 'unknown';
      await this.client.call<WorkView>('continuation_save', { request_id: requestId, draft: {
        task_id: taskId, branch_id: sessionId, expected_revision: previous?.revision ?? 0, anchor_id: snapshot.reference[0],
        goal, progress: parts.progress.length ? parts.progress : [snapshot.span()], pending: state === 'completed' ? [] : parts.pending,
        state, expires_at: new Date(summary.time + 7 * 86400000).toISOString(), dependencies,
        consumed_checkpoints: input.consumed_checkpoints, goal_role: 'initial_user_request' } });
      this.errors.delete(sessionId);
    } catch (error) {
      this.errors.set(sessionId, error instanceof Error && /^evernook:[a-z_]+$/.test(error.message)
        ? error.message.slice('evernook:'.length) : 'continuation_unavailable');
    }
  }

  current(sessionId: string): Promise<WorkView | null> {
    if (this.errors.has(sessionId)) return Promise.resolve(null);
    const taskId = this.keys.get(sessionId);
    return taskId ? currentWork(this.client, taskId, sessionId) : Promise.resolve(null);
  }
  error(sessionId: string) { return this.errors.get(sessionId); }
}
