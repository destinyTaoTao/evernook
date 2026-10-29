import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type { Context } from '@deepseek-ai/cordis';
import { isAgentLoopRequest, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { assertExternalPath, type Evidence } from './host.ts';

export interface PersistedChunk { attempt: string; ordinal: number; chunk: StreamChunk }

/** R0 only: fixed real-core composition journals before the native live-publication boundary. */
export function installDisplayBarrier(ctx: Context, evidence: Evidence) {
  return ctx.on('llm/stream', async function* (options, next) {
    if (!isAgentLoopRequest(options) || !options.sessionId) throw new Error('Display probe requires an identified real agent-loop request');
    const attempt = randomUUID();
    let ordinal = 0;
    for await (const chunk of next()) {
      options.signal?.throwIfAborted();
      // Synchronous FULL transaction completes before returning this chunk to dsh's LiveAttempt.push.
      evidence.persistChunk(options.sessionId, attempt, ordinal++, chunk);
      yield chunk;
    }
  });
}

/** Independent read-only connection, also usable after forced exit without a live host. */
export function readPersistedChunks(root: string, sessionId: string): PersistedChunk[] {
  const path = join(root, 'evidence.sqlite');
  assertExternalPath(path);
  const db = new DatabaseSync(path, { readOnly: true });
  try {
    return db.prepare('SELECT attempt, ordinal, chunk FROM stream_chunks WHERE session = ? ORDER BY attempt, ordinal').all(sessionId)
      .map(row => ({ attempt: String(row.attempt), ordinal: Number(row.ordinal), chunk: JSON.parse(String(row.chunk)) as StreamChunk }));
  } finally { db.close(); }
}
