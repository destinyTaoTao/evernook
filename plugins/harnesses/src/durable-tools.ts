import type { StreamChunk } from '@deepseek-ai/dsh-llm';
import type { Event, MemoryClient } from './client.ts';

/** Text prefixes stream through Core; structural/tool data waits for the actual provider finish.
 * No reasoning, attachments, overlapping text blocks, or invented finish frames. */
export async function* durableTools(client: MemoryClient, incoming: AsyncIterable<StreamChunk>,
    template: Event, inputId: string, maxBytes: number, signal?: AbortSignal): AsyncIterable<StreamChunk> {
  type Block = { type: 'text' | 'tool-call'; raw: string; high: string; ended: boolean; id?: string; name?: string };
  type Queued = { frame: StreamChunk; structural: boolean; start?: number; points?: string[]; sent?: number };
  const blocks = new Map<number, Block>();
  const frames: StreamChunk[] = [];
  const queue: Queued[] = [];
  let bytes = 0;
  let chunkIndex = 0;
  let carry = '';
  let state = 'open';
  let initialized = false;
  let uncertain = false;
  let total = 0;
  let released = 0;
  let sealed = false;

  async function commit(text: string, completion: Event['completion']) {
    signal?.throwIfAborted();
    uncertain = true;
    const receipt = await client.appendChunk({ ...template, text, completion }, chunkIndex, carry, inputId);
    uncertain = false; initialized = true; chunkIndex++; state = receipt.state;
    const combined = Array.from(carry + text);
    carry = combined.slice(receipt.released_chars).join('');
    if (state === 'protected') throw new Error('evernook:protected_stream');
    released += receipt.released_chars;
    signal?.throwIfAborted();
  }
  function* drain(): Iterable<StreamChunk> {
    while (queue.length) {
      signal?.throwIfAborted();
      const item = queue[0];
      if (item.structural && !sealed) return;
      if (item.points) {
        const count = Math.max(0, Math.min(item.points.length, released - item.start!));
        const text = item.points.slice(item.sent!, count).join('');
        item.sent = Math.max(item.sent!, count);
        if (text && item.frame.type === 'text-delta') yield { type: 'text-delta', index: item.frame.index, text };
        if (item.sent < item.points.length) return;
      } else if (item.frame.type === 'block-start' && item.frame.blockType === 'text') {
        yield { type: 'block-start', index: item.frame.index, blockType: 'text' };
      } else if (item.frame.type === 'block-end' && item.frame.block.type === 'text') {
        yield { type: 'block-end', index: item.frame.index, block: { type: 'text', text: item.frame.block.text } };
      } else yield item.frame;
      queue.shift();
    }
  }
  try {
    await commit('', 'partial');
    for await (const supplied of incoming) {
      signal?.throwIfAborted();
      // Detach from provider-owned objects, including replay metadata.
      const json = JSON.stringify(supplied);
      bytes += Buffer.byteLength(json, 'utf8') + 1;
      if (bytes > maxBytes) throw new Error('evernook:stream_too_large');
      const frame = JSON.parse(json) as StreamChunk;
      frames.push(JSON.parse(json) as StreamChunk);
      if (frame.type === 'finish') {
        if (!blocks.size || [...blocks.values()].some(block => !block.ended)
            || !['stop', 'tool-calls'].includes(frame.reason.kind)) throw new Error('evernook:incomplete_tool_stream');
        const tools = [...blocks.values()].filter(block => block.type === 'tool-call');
        if ((tools.length > 0) !== (frame.reason.kind === 'tool-calls')
            || new Set(tools.map(block => block.id)).size !== tools.length) throw new Error('evernook:invalid_tool_stream');
        // Decode JSON before screening too: escaped keys must not hide credentials.
        const argumentsText = tools.map(block => JSON.stringify(JSON.parse(block.raw)));
        const text = [...blocks.values()].filter(block => block.type === 'text').map(block => block.raw).join('');
        const raw = Buffer.from(JSON.stringify(frames), 'utf8');
        const audit = { ...template, source: { ...template.source, event: template.source.event + ':frames' },
          role: 'system' as const, completion: 'complete' as const,
          text: 'evernook_dsh_tool_frames_v1\n' + text + '\n' + tools.map(block => block.name).join('\n') + '\n' + argumentsText.join('\n') };
        const receipt = await client.capture(audit, 'message', [{ attachment_id: 'raw-tool-stream-v1', label: 'raw-tool-stream.json',
          media_type: 'application/json', chunks_base64: [raw.toString('base64')], expected_size: raw.length, complete: true }], inputId);
        if (!receipt.message.original_saved || !receipt.complete) throw new Error('evernook:stream_not_fully_saved');
        await commit('', 'complete');
        if (released !== total || carry) throw new Error('evernook:stream_text_mismatch');
        sealed = true;
        queue.push({ frame, structural: true });
        yield* drain();
        return;
      }
      if (frame.type === 'usage') { queue.push({ frame, structural: true }); continue; }
      if (!Number.isSafeInteger(frame.index) || frame.index < 0 || frame.index > 65536) throw new Error('evernook:invalid_tool_stream');
      if (frame.type === 'block-start') {
        if (!['text', 'tool-call'].includes(frame.blockType) || blocks.has(frame.index)
            || [...blocks.keys()].some(index => index >= frame.index)
            || [...blocks.values()].some(block => !block.ended && (block.type === 'text' || frame.blockType === 'text'))) {
          throw new Error('evernook:unsupported_tool_stream');
        }
        const type = frame.blockType as Block['type'];
        blocks.set(frame.index, { type, raw: '', high: '', ended: false });
        queue.push({ frame, structural: type !== 'text' });
      } else {
        const block = blocks.get(frame.index);
        if (!block || block.ended) throw new Error('evernook:invalid_tool_stream');
        if (frame.type === 'text-delta' && block.type === 'text') {
          block.raw += frame.text;
          let text = block.high + frame.text;
          block.high = /[\uD800-\uDBFF]$/.test(text) ? text.at(-1)! : '';
          if (block.high) text = text.slice(0, -1);
          if (!text.isWellFormed()) throw new Error('evernook:invalid_tool_stream');
          const points = Array.from(text);
          if (points.length) queue.push({ frame, structural: false, start: total, points, sent: 0 });
          total += points.length;
          let piece = ''; let size = 0;
          for (const point of points) {
            const width = Buffer.byteLength(point, 'utf8');
            if (size + width > 65536) { await commit(piece, 'partial'); yield* drain(); piece = ''; size = 0; }
            piece += point; size += width;
          }
          if (piece) await commit(piece, 'partial');
        } else if (frame.type === 'tool-call-delta' && block.type === 'tool-call') {
          if (typeof frame.id !== 'string' || !frame.id || block.id && block.id !== frame.id
              || frame.name !== undefined && (typeof frame.name !== 'string' || !frame.name || block.name && block.name !== frame.name)) {
            throw new Error('evernook:invalid_tool_stream');
          }
          block.id = frame.id;
          if (frame.name) block.name = frame.name;
          block.raw += frame.argumentsDelta;
          queue.push({ frame, structural: true });
        } else if (frame.type === 'block-end') {
          if (block.type === 'text') {
            if (frame.block.type !== 'text' || frame.block.text !== block.raw || block.high) throw new Error('evernook:stream_text_mismatch');
          } else if (frame.block.type !== 'tool-call' || frame.block.id !== block.id || frame.block.name !== block.name
              || frame.block.arguments !== block.raw) throw new Error('evernook:invalid_tool_stream');
          block.ended = true;
          queue.push({ frame, structural: block.type !== 'text' });
        } else throw new Error('evernook:unsupported_tool_stream');
      }
      yield* drain();
    }
    throw new Error('evernook:incomplete_tool_stream');
  } catch {
    // Parser/provider diagnostics may contain arguments that have not passed screening.
    throw new Error(signal?.aborted ? 'evernook:operation_canceled' : 'evernook:tool_stream_publication_failed');
  } finally {
    if (initialized && !uncertain && state === 'open') {
      await client.appendChunk({ ...template, text: '', completion: signal?.aborted ? 'canceled' : 'failed' }, chunkIndex, '', inputId);
    }
  }
}
