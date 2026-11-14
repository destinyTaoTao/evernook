import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';
import type { Source } from './client.ts';

export interface TranscriptionMaterial {
  source: Source;
  sequence: number;
  occurred_at: string | null;
  completion: 'complete' | 'partial' | 'canceled' | 'failed';
  text: string;
  provider: string;
  model: string;
  language: string;
  execution: 'local' | 'remote';
  start_ms: number;
  end_ms: number;
  confidence?: number | null;
  sensitive?: boolean;
  speaker?: 'user' | 'assistant' | 'unknown';
}

export interface TranscriptionClientConfig { python: string; config: string; access: string; cwd?: string }
type Result = Record<string, unknown>;
const object = (value: unknown): value is Result => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Trusted host only. No audio, provider credentials or MCP tools are involved. */
export class TranscriptionClient {
  private readonly config: TranscriptionClientConfig;
  constructor(config: TranscriptionClientConfig) {
    if (![config.python, config.config, config.access].every(isAbsolute)) throw new Error('evernook:invalid_host_config');
    this.config = config;
  }

  async submit(material: TranscriptionMaterial, signal?: AbortSignal): Promise<Result> {
    const result = await this.invoke('service-transcription-submit', [], material, signal);
    if (!result) throw new Error('evernook:transcription_unavailable');
    return result;
  }

  read(messageId: string, signal?: AbortSignal): Promise<Result | null> {
    return this.invoke('service-transcription-read', ['--message-id', messageId], {}, signal);
  }

  private invoke(operation: string, extra: string[], input: unknown, signal?: AbortSignal): Promise<Result | null> {
    if (signal?.aborted) return Promise.reject(new Error('evernook:operation_canceled'));
    return new Promise((resolve, reject) => {
      const child = spawn(this.config.python, ['-B', '-m', 'evernook.cli', operation, '--config', this.config.config,
        '--access', this.config.access, '--require-service', ...extra],
      { cwd: this.config.cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'] });
      let chunks: Buffer[] = []; let size = 0; let failure: string | undefined;
      const stop = (reason: string) => { failure ??= reason; child.kill(); };
      const abort = () => stop('operation_canceled');
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
      const timer = setTimeout(() => stop('transcription_timeout'), 30000);
      child.stdout.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size > 8 * 1024 * 1024) { chunks = []; stop('transcription_result_too_large'); }
        else if (!failure) chunks.push(chunk);
      });
      child.stdout.on('error', () => stop('transcription_unavailable'));
      child.stdin.on('error', () => { failure ??= 'transcription_unavailable'; });
      child.on('error', () => { failure ??= 'transcription_unavailable'; });
      child.on('close', code => {
        clearTimeout(timer); signal?.removeEventListener('abort', abort);
        if (failure) { reject(new Error('evernook:' + failure)); return; }
        try {
          const payload: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
          chunks = [];
          if (!object(payload)) throw new Error();
          if (code !== 0 || payload.ok !== true) {
            const reason = object(payload.error) && typeof payload.error.code === 'string' && /^[a-z_]{1,80}$/.test(payload.error.code)
              ? payload.error.code : 'transcription_unavailable';
            reject(new Error('evernook:' + reason)); return;
          }
          if (payload.result !== null && !object(payload.result)) throw new Error();
          resolve(payload.result);
        } catch { reject(new Error('evernook:transcription_unavailable')); }
      });
      try { child.stdin.end(JSON.stringify(input)); }
      catch { stop('transcription_unavailable'); }
    });
  }
}
