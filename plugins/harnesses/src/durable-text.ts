import type { StreamChunk } from '@deepseek-ai/dsh-llm';
import type { Event, MemoryClient } from './client.ts';

/** One text block only. Raw provider metadata and unsupported content never cross this barrier. */
export async function* durableText(client: MemoryClient, incoming: AsyncIterable<StreamChunk>,
    template: Event, inputId: string, maxBytes: number, signal?: AbortSignal): AsyncIterable<StreamChunk> {
  let index = 0;
  let carry = '';
  let state = 'open';
  let uncertain = false;
  let initialized = false;
  let started = false;
  let ended = false;
  let bytes = 0;
  let raw = '';
  let published = '';
  async function commit(text: string, completion: Event['completion']) {
    signal?.throwIfAborted();
    uncertain = true;
    // Do not cancel an in-flight commit: cancellation stops publication, not reconciliation.
    const receipt = await client.appendChunk({ ...template, text, completion }, index, carry, inputId);
    uncertain = false;
    initialized = true;
    index++;
    state = receipt.state;
    const combined = Array.from(carry + text);
    const released = combined.slice(0, receipt.released_chars).join('');
    carry = combined.slice(receipt.released_chars).join('');
    if (state === 'protected') throw new Error('evernook:protected_stream');
    signal?.throwIfAborted();
    published += released;
    return released;
  }
  try {
    await commit('', 'partial');
    for await (const frame of incoming) {
      signal?.throwIfAborted();
      bytes += Buffer.byteLength(JSON.stringify(frame), 'utf8') + 1;
      if (bytes > maxBytes) throw new Error('evernook:stream_too_large');
      if (frame.type === 'block-start' && !started && frame.index === 0 && frame.blockType === 'text') {
        started = true;
        yield { type: 'block-start', index: 0, blockType: 'text' };
      } else if (frame.type === 'text-delta' && started && !ended && frame.index === 0) {
        raw += frame.text;
        // A provider may split a UTF-16 surrogate pair between deltas. Keep it out of UTF-8 transport.
        let text = frame.text;
        if (raw.length > text.length && /[\uD800-\uDBFF]$/.test(raw.slice(0, -text.length))) {
          text = raw.at(-(text.length + 1))! + text;
        }
        if (/[\uD800-\uDBFF]$/.test(text)) text = text.slice(0, -1);
        // Bound each core request by UTF-8 bytes, independently of provider delta sizes.
        let piece = '';
        let size = 0;
        for (const point of text) {
          const width = Buffer.byteLength(point, 'utf8');
          if (size + width > 65536) {
            const released = await commit(piece, 'partial');
            if (released) yield { type: 'text-delta', index: 0, text: released };
            piece = ''; size = 0;
          }
          piece += point; size += width;
        }
        if (piece) {
          const released = await commit(piece, 'partial');
          if (released) yield { type: 'text-delta', index: 0, text: released };
        }
      } else if (frame.type === 'block-end' && started && !ended && frame.index === 0
          && frame.block.type === 'text' && frame.block.text === raw && !/[\uD800-\uDBFF]$/.test(raw)) {
        // The final block can contain a withheld sensitive tail. Hold it until normal finish.
        ended = true;
      } else if (frame.type === 'usage') {
        // Provider metadata is outside the text persistence contract and is not published.
      } else if (frame.type === 'finish') {
        if (frame.reason.kind === 'error' || frame.reason.kind === 'aborted') {
          throw new Error('evernook:provider_stream_failed');
        }
        if (!started || !ended || !['stop', 'max-tokens'].includes(frame.reason.kind)) {
          throw new Error('evernook:unsupported_text_stream');
        }
        const released = await commit('', frame.reason.kind === 'max-tokens' ? 'failed' : 'complete');
        if (released) yield { type: 'text-delta', index: 0, text: released };
        yield { type: 'block-end', index: 0, block: { type: 'text', text: published } };
        yield { type: 'finish', reason: { kind: frame.reason.kind } };
        return;
      } else throw new Error('evernook:unsupported_text_stream');
    }
    throw new Error('evernook:incomplete_text_stream');
  } finally {
    if (initialized && !uncertain && state === 'open') {
      // An ambiguous transport outcome stays open for read_stream reconciliation; never change its request.
      await client.appendChunk({ ...template, text: '', completion: signal?.aborted ? 'canceled' : 'failed' },
        index, '', inputId);
    }
  }
}
