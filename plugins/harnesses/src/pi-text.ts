import type { AssistantMessageEvent } from '@earendil-works/pi-ai';
import type { StreamChunk } from '@deepseek-ai/dsh-llm';

/** Convert only the supported text protocol; provider partials and metadata are never forwarded. */
export async function* piTextFrames(events: AsyncIterable<AssistantMessageEvent>, onDelta: () => void): AsyncIterable<StreamChunk> {
  let text = '';
  let started = false;
  for await (const frame of events) {
    if (frame.type === 'start' && !started) { started = true; continue; }
    if (!started) throw new Error('evernook:unsupported_text_stream');
    if (frame.type === 'text_start') yield { type: 'block-start', index: frame.contentIndex, blockType: 'text' };
    else if (frame.type === 'text_delta') {
      if (frame.delta) onDelta();
      text += frame.delta;
      yield { type: 'text-delta', index: frame.contentIndex, text: frame.delta };
    } else if (frame.type === 'text_end') yield { type: 'block-end', index: frame.contentIndex, block: { type: 'text', text: frame.content } };
    else if (frame.type === 'done') {
      if (frame.message.content.length !== 1 || frame.message.content[0].type !== 'text'
          || frame.message.content[0].text !== text || frame.message.stopReason !== frame.reason
          || !['stop', 'length'].includes(frame.reason)) throw new Error('evernook:unsupported_text_stream');
      yield { type: 'finish', reason: { kind: frame.reason === 'length' ? 'max-tokens' : 'stop' } };
      return;
    } else if (frame.type === 'error') throw new Error('evernook:provider_stream_failed');
    else throw new Error('evernook:unsupported_text_stream');
  }
  throw new Error('evernook:incomplete_text_stream');
}
